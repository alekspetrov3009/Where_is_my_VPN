"""Экспорт рабочего пула в форматы клиентов: Clash/Mihomo YAML и Xray JSON.

Clash/Mihomo YAML — самый распространённый формат подписок (Clash Verge,
Mihomo, FlClash, Stash). Собирается без внешних зависимостей: YAML пишется
вручную, значения экранируются, поэтому генератор детерминирован и не тянет
PyYAML в сборку .exe.

Схема Clash отличается от sing-box: имена полей и структура транспорта иные
(cipher вместо method, ws-opts вместо transport, reality-opts и т.д.),
поэтому это отдельный конвертер, а не переименование ключей.
"""
import base64
import json
from urllib.parse import urlparse, parse_qs, unquote

# Прокси-группы: auto — urltest (Clash сам выбирает живой сервер),
# select — ручной выбор. Обе смотрят на один список серверов.
GROUP_AUTO = "auto"
GROUP_SELECT = "select"


def _yaml_str(value):
    """Строка YAML в двойных кавычках с экранированием.

    Имена серверов приходят из чужих remark и содержат эмодзи, двоеточия и
    кавычки — без экранирования YAML ломается.
    """
    text = str(value if value is not None else "")
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return f'"{escaped}"'


def _yaml_scalar(value):
    """Скаляр YAML: bool/int/float как есть, строка — в кавычках.

    dict/list сериализуются как inline-коллекции YAML, а не repr Python:
    ws-opts/grpc-opts/reality-opts — вложенные структуры, и repr вида
    {'path': '/'} даёт невалидный YAML, на котором Clash не стартует.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_yaml_scalar(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(
            f"{_yaml_str(key)}: {_yaml_scalar(item)}" for key, item in value.items()) + "}"
    return _yaml_str(value)


def _safe_name(uri, index):
    """Имя сервера: remark из URI, иначе протокол и индекс.

    Двоеточия в имени ломают разбор Clash, поэтому заменяются на дефис.
    """
    fragment = uri.split("#", 1)[1] if "#" in uri else ""
    name = unquote(fragment).strip() if fragment else ""
    if not name:
        name = f"{uri.split('://', 1)[0]}-{index}"
    name = name.replace(":", "-").replace(",", " ")
    return name[:60]


def _query(parsed):
    """get_q-совместимый доступ к query-параметрам URI."""
    params = parse_qs(parsed.query)

    def get_q(key, default=""):
        return params.get(key, [default])[0]

    return get_q


def _clash_vless(parsed, get_q, name):
    proxy = {"name": name, "type": "vless", "server": parsed.hostname,
             "port": parsed.port, "uuid": unquote(parsed.username or ""),
             "udp": True}
    network = get_q("type", "tcp")
    if network in ("ws", "grpc", "http", "h2"):
        proxy["network"] = "ws" if network == "ws" else network
    if network == "ws":
        proxy["ws-opts"] = {"path": get_q("path", "/"),
                            "headers": {"Host": get_q("host", parsed.hostname)}}
    elif network == "grpc":
        proxy["grpc-opts"] = {"grpc-service-name": get_q("serviceName")}
    flow = get_q("flow")
    if flow:
        proxy["flow"] = flow
    security = get_q("security")
    if security in ("tls", "reality"):
        proxy["tls"] = True
        proxy["servername"] = get_q("sni", parsed.hostname)
        proxy["skip-cert-verify"] = True
        fingerprint = get_q("fp")
        if fingerprint:
            proxy["client-fingerprint"] = fingerprint
        if security == "reality":
            proxy["reality-opts"] = {"public-key": get_q("pbk"), "short-id": get_q("sid")}
    return proxy


def _clash_trojan(parsed, get_q, name):
    proxy = {"name": name, "type": "trojan", "server": parsed.hostname,
             "port": parsed.port, "password": unquote(parsed.username or ""),
             "udp": True, "skip-cert-verify": True,
             "sni": get_q("sni", parsed.hostname)}
    network = get_q("type", "tcp")
    if network == "ws":
        proxy["network"] = "ws"
        proxy["ws-opts"] = {"path": get_q("path", "/"),
                            "headers": {"Host": get_q("host", parsed.hostname)}}
    elif network == "grpc":
        proxy["network"] = "grpc"
        proxy["grpc-opts"] = {"grpc-service-name": get_q("serviceName")}
    return proxy


def _clash_ss(uri, parsed, name):
    """Shadowsocks: три формы записи, как в config_parser._parse_ss."""
    raw_authority = uri[5:].split("#", 1)[0].split("?", 1)[0].split("/", 1)[0]
    host, port = parsed.hostname, None
    try:
        port = parsed.port
    except ValueError:
        port = None
    if not host or not port or "@" not in raw_authority:
        body = raw_authority + "=" * ((4 - len(raw_authority) % 4) % 4)
        try:
            decoded = base64.urlsafe_b64decode(body).decode("utf-8")
            reparsed = urlparse("ss://" + decoded)
        except Exception:
            return None
        if not reparsed.hostname or not reparsed.port:
            return None
        method, password = _split_ss_credentials(unquote(reparsed.username or ""),
                                                 reparsed.password)
        host, port = reparsed.hostname, reparsed.port
    else:
        method, password = _split_ss_credentials(unquote(parsed.username or ""),
                                                 parsed.password)
    if not method or not password:
        return None
    return {"name": name, "type": "ss", "server": host, "port": port,
            "cipher": method, "password": password, "udp": True}


def _split_ss_credentials(userpass, password_part):
    """(method, password) из userinfo: либо method:pass, либо base64 целиком."""
    if password_part is not None:
        return userpass, unquote(password_part)
    try:
        decoded = base64.urlsafe_b64decode(userpass + "===").decode("utf-8")
        method, password = decoded.split(":", 1)
        return method, password
    except Exception:
        return None, None


def _clash_vmess(uri, name):
    """vmess://BASE64-JSON — адрес и порт внутри тела."""
    try:
        body = uri[8:].split("#", 1)[0].strip()
        body = body.replace("-", "+").replace("_", "/")
        body += "=" * ((4 - len(body) % 4) % 4)
        data = json.loads(base64.b64decode(body, validate=False).decode("utf-8"))
    except Exception:
        return None
    if not data.get("add") or not data.get("id"):
        return None
    try:
        port = int(data.get("port", 443))
    except (TypeError, ValueError):
        return None
    proxy = {"name": name, "type": "vmess", "server": data["add"], "port": port,
             "uuid": data["id"], "alterId": int(data.get("aid", 0) or 0),
             "cipher": data.get("scy", "auto") or "auto", "udp": True}
    if data.get("tls") == "tls":
        proxy["tls"] = True
        proxy["servername"] = data.get("sni", data.get("add"))
        proxy["skip-cert-verify"] = True
    network = data.get("net", "tcp")
    if network == "ws":
        proxy["network"] = "ws"
        proxy["ws-opts"] = {"path": data.get("path", "/"),
                            "headers": {"Host": data.get("host", data.get("add"))}}
    elif network == "grpc":
        proxy["network"] = "grpc"
        proxy["grpc-opts"] = {"grpc-service-name": data.get("path", "")}
    return proxy


def _clash_hysteria2(parsed, get_q, name):
    proxy = {"name": name, "type": "hysteria2", "server": parsed.hostname,
             "port": parsed.port,
             "password": unquote(parsed.username or "") or get_q("password"),
             "sni": get_q("sni", parsed.hostname), "skip-cert-verify": True}
    if get_q("obfs") == "salamander":
        obfs_password = get_q("obfs-password") or get_q("obfs_password")
        if obfs_password:
            proxy["obfs"] = "salamander"
            proxy["obfs-password"] = obfs_password
    return proxy


def _clash_tuic(parsed, get_q, name):
    password = unquote(parsed.password or "") or get_q("password")
    uuid = unquote(parsed.username or "")
    if not uuid or not password:
        return None
    proxy = {"name": name, "type": "tuic", "server": parsed.hostname,
             "port": parsed.port, "uuid": uuid, "password": password,
             "sni": get_q("sni", parsed.hostname), "skip-cert-verify": True}
    congestion = get_q("congestion_control") or get_q("congestion")
    if congestion:
        proxy["congestion-controller"] = congestion
    udp_mode = get_q("udp_relay_mode")
    if udp_mode:
        proxy["udp-relay-mode"] = udp_mode
    alpn = get_q("alpn")
    if alpn:
        proxy["alpn"] = [item for item in alpn.split(",") if item]
    return proxy


def uri_to_clash_proxy(uri, index):
    """Один URI → dict прокси Clash/Mihomo; None, если протокол не поддержан."""
    try:
        parsed = urlparse(uri)
        scheme = parsed.scheme
        if scheme not in ("vless", "trojan", "ss", "vmess", "hysteria2", "tuic"):
            return None
        name = _safe_name(uri, index)
        if scheme in ("vmess", "ss"):
            if scheme == "vmess":
                return _clash_vmess(uri, name)
            return _clash_ss(uri, parsed, name)
        if not parsed.hostname or not parsed.port:
            return None
        get_q = _query(parsed)
        builders = {
            "vless": lambda: _clash_vless(parsed, get_q, name),
            "trojan": lambda: _clash_trojan(parsed, get_q, name),
            "hysteria2": lambda: _clash_hysteria2(parsed, get_q, name),
            "tuic": lambda: _clash_tuic(parsed, get_q, name),
        }
        return builders[scheme]()
    except Exception:
        return None


def build_clash_config(uris, mixed_port=7890):
    """Собирает Clash/Mihomo YAML из списка URI.

    Возвращает (yaml_text, error). Прокси с неподдерживаемым протоколом
    пропускаются; если не осталось ни одного — это ошибка, а не пустой файл.
    """
    proxies = []
    for index, uri in enumerate(uris, 1):
        proxy = uri_to_clash_proxy(uri, index)
        if proxy:
            proxies.append(proxy)
    if not proxies:
        return None, "Ни один URI не удалось преобразовать в формат Clash"

    names = [proxy["name"] for proxy in proxies]
    # Дубли имён ломают ссылки групп: разводим суффиксом.
    seen = {}
    for proxy in proxies:
        base = proxy["name"]
        if base in seen:
            seen[base] += 1
            proxy["name"] = f"{base} #{seen[base]}"
        else:
            seen[base] = 1
    names = [proxy["name"] for proxy in proxies]

    lines = [
        "# Where is my VPN — конфигурация Clash/Mihomo",
        "# Сгенерировано автоматически из проверенного пула.",
        "mixed-port: %d" % mixed_port,
        "allow-lan: false",
        "mode: rule",
        "log-level: warning",
        "dns:",
        "  enable: true",
        "  nameserver:",
        "    - 8.8.8.8",
        "    - 1.1.1.1",
        "proxies:",
    ]
    for proxy in proxies:
        lines.append(f"  - name: {_yaml_str(proxy['name'])}")
        for key, value in proxy.items():
            if key == "name":
                continue
            lines.append(f"    {key}: {_yaml_scalar(value)}")
    lines.append("proxy-groups:")
    for group_name, group_type in ((GROUP_AUTO, "url-test"), (GROUP_SELECT, "select")):
        lines.append(f"  - name: {_yaml_str(group_name)}")
        lines.append(f"    type: {group_type}")
        lines.append("    proxies:")
        for name in names:
            lines.append(f"      - {_yaml_str(name)}")
        if group_type == "url-test":
            lines.append("    url: http://cp.cloudflare.com")
            lines.append("    interval: 300")
            lines.append("    tolerance: 50")
    lines.append("rules:")
    lines.append(f"  - MATCH,{GROUP_AUTO}")
    return "\n".join(lines) + "\n", None