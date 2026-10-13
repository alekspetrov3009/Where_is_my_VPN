"""Парсеры VPN-URI в outbound-конфигурации sing-box.

Поддерживаются vless, trojan, shadowsocks, vmess, hysteria2 и tuic.
hysteria2 разбирается вместе с obfs (salamander), tuic — с congestion_control,
udp_relay_mode и alpn: без этих параметров рабочие серверы ложно бракуются.
"""
import base64
import json
from urllib.parse import urlparse, parse_qs, unquote

# Префиксы URI, которые собираются из источников и допускаются в пул.
URI_PREFIXES = ('vless://', 'trojan://', 'ss://', 'hysteria2://', 'tuic://', 'vmess://')


def _build_transport(transport_type, get_q, hostname):
    """Транспортный слой для vless/trojan: grpc, ws или http/h2; tcp — без транспорта."""
    if transport_type == "grpc":
        return {"type": "grpc", "service_name": get_q("serviceName", "")}
    if transport_type == "ws":
        return {"type": "ws",
                "path": get_q("path", "/"),
                "headers": {"Host": get_q("host", hostname)}}
    if transport_type in ("http", "h2"):
        return {"type": "http",
                "host": [get_q("host", hostname)],
                "path": get_q("path", "/")}
    return None


def _parse_vless(parsed, get_q, tag):
    """vless://uuid@host:port?flow=...&security=tls|reality&type=..."""
    uuid = unquote(parsed.username or "")
    if not uuid:
        return None
    outbound = {
        "type": "vless", "tag": tag,
        "server": parsed.hostname, "server_port": parsed.port,
        "uuid": uuid,
    }
    flow = get_q("flow")
    if flow:
        outbound["flow"] = flow
    security = get_q("security")
    if security in ["tls", "reality"]:
        tls_obj = {
            "enabled": True,
            "server_name": get_q("sni", parsed.hostname),
            "utls": {"enabled": True, "fingerprint": get_q("fp", "chrome")}
        }
        if security == "reality":
            tls_obj["reality"] = {
                "enabled": True,
                "public_key": get_q("pbk"),
                "short_id": get_q("sid")
            }
        outbound["tls"] = tls_obj
    transport = _build_transport(get_q("type", "tcp"), get_q, parsed.hostname)
    if transport:
        outbound["transport"] = transport
    return outbound


def _parse_trojan(parsed, get_q, tag):
    """trojan://password@host:port?sni=...&type=... — TLS включён всегда."""
    password = unquote(parsed.username or "")
    if not password:
        return None
    outbound = {
        "type": "trojan", "tag": tag,
        "server": parsed.hostname, "server_port": parsed.port,
        "password": password,
        "tls": {"enabled": True, "server_name": get_q("sni", parsed.hostname),
                "utls": {"enabled": True, "fingerprint": get_q("fp", "chrome")}}
    }
    transport = _build_transport(get_q("type", "tcp"), get_q, parsed.hostname)
    if transport:
        outbound["transport"] = transport
    return outbound


def _split_credentials(userpass, password_part):
    """(method, password) из userinfo URI.

    Пароль либо в password-части URI (method:pass открытым текстом),
    либо всё userpass — base64 от "method:password". При неудаче — (None, None).
    """
    if password_part is not None:
        return userpass, unquote(password_part)
    try:
        decoded = base64.urlsafe_b64decode(userpass + "===").decode('utf-8')
        method, password = decoded.split(":", 1)
        return method, password
    except Exception:
        return None, None


def _parse_ss(uri, parsed, tag):
    """Shadowsocks. Форматы: ss://b64(method:pass)@host:port,
    ss://b64(method:pass@host:port), ss://method:pass@host:port."""
    raw_authority = uri[5:].split('#', 1)[0].split('?', 1)[0].split('/', 1)[0]
    host, port = parsed.hostname, None
    try:
        port = parsed.port
    except ValueError:
        port = None
    if not host or not port or "@" not in raw_authority:
        # Целиком base64 в authority: ss://W0d...#tag
        b64_body = raw_authority
        b64_body += "=" * ((4 - len(b64_body) % 4) % 4)
        try:
            decoded_full = base64.urlsafe_b64decode(b64_body).decode('utf-8')
        except Exception:
            return None
        if "@" not in decoded_full or ":" not in decoded_full:
            return None
        # Перепарсиваем как обычный URI после декодирования.
        reparsed = urlparse("ss://" + decoded_full)
        if not reparsed.hostname or not reparsed.port:
            return None
        method, password = _split_credentials(unquote(reparsed.username or ""),
                                              reparsed.password)
        if method is None:
            return None
        host, port = reparsed.hostname, reparsed.port
    else:
        method, password = _split_credentials(unquote(parsed.username or ""),
                                              parsed.password)
        if method is None:
            return None
    if not method or not password:
        return None
    return {
        "type": "shadowsocks", "tag": tag,
        "server": host, "server_port": port,
        "method": method, "password": password
    }


def _parse_vmess(uri, tag):
    """vmess://BASE64-JSON: адрес, порт и uuid внутри тела, не в authority."""
    try:
        b64_str = uri[8:].split('#', 1)[0].strip()
        # Принимаем и standard (+/), и urlsafe (-_) base64.
        b64_str = b64_str.replace('-', '+').replace('_', '/')
        b64_str += "=" * ((4 - len(b64_str) % 4) % 4)
        vmess_json = json.loads(base64.b64decode(b64_str, validate=False).decode('utf-8'))
        if not isinstance(vmess_json.get("add"), str) or not vmess_json["add"] or not vmess_json.get("id"):
            return None
        if not 1 <= int(vmess_json.get("port", 443)) <= 65535:
            return None
        outbound = {
            "type": "vmess", "tag": tag,
            "server": vmess_json.get("add"),
            "server_port": int(vmess_json.get("port", 443)),
            "uuid": vmess_json.get("id"),
            "security": vmess_json.get("scy", "auto"),
            "alter_id": int(vmess_json.get("aid", 0)),
        }
        if vmess_json.get("tls") == "tls":
            outbound["tls"] = {
                "enabled": True,
                "server_name": vmess_json.get("sni", vmess_json.get("add")),
                "insecure": True
            }
        net = vmess_json.get("net", "tcp")
        if net == "ws":
            outbound["transport"] = {
                "type": "ws",
                "path": vmess_json.get("path", "/"),
                "headers": {"Host": vmess_json.get("host", vmess_json.get("add"))}
            }
        elif net == "grpc":
            outbound["transport"] = {"type": "grpc", "service_name": vmess_json.get("path", "")}
        return outbound
    except Exception:
        return None


def _parse_hysteria2(parsed, get_q, tag):
    """hysteria2://password@host:port?sni=...&obfs=salamander&obfs-password=... — TLS всегда включён.

    obfs (salamander) критичен для обхода DPI: без него сервер с включённой
    обфускацией не принимает соединение, и рабочий ключ ложно попадает в
    «нерабочие». Поддерживаются оба написания параметра: obfs-password и
    obfs_password (встречаются в разных генераторах ссылок).
    """
    password = unquote(parsed.username or "") or get_q("password", unquote(parsed.password or ""))
    if not password:
        return None
    outbound = {
        "type": "hysteria2", "tag": tag,
        "server": parsed.hostname, "server_port": parsed.port,
        "password": password,
        "tls": {"enabled": True, "server_name": get_q("sni", parsed.hostname), "insecure": True}
    }
    obfs_type = get_q("obfs")
    if obfs_type:
        obfs_password = get_q("obfs-password") or get_q("obfs_password")
        # salamander — единственный тип обфускации в sing-box 1.10.x;
        # неизвестный тип не подставляем, иначе sing-box отвергнет весь конфиг.
        if obfs_type == "salamander" and obfs_password:
            outbound["obfs"] = {"type": "salamander", "password": obfs_password}
    return outbound


def _parse_tuic(parsed, get_q, tag):
    """tuic://uuid:password@host:port?sni=...&congestion_control=...&alpn=...

    TUIC работает поверх QUIC (UDP) и часто проходит там, где TCP-протоколы
    режутся DPI. Пароль может быть в userinfo (uuid:password) либо отдельным
    параметром password= (некоторые генераторы ссылок кладут только uuid).
    """
    uuid = unquote(parsed.username or "")
    if not uuid:
        return None
    password = unquote(parsed.password or "") or get_q("password")
    if not password:
        return None
    outbound = {
        "type": "tuic", "tag": tag,
        "server": parsed.hostname, "server_port": parsed.port,
        "uuid": uuid, "password": password,
        "tls": {"enabled": True, "server_name": get_q("sni", parsed.hostname),
                "insecure": True}
    }
    congestion = get_q("congestion_control") or get_q("congestion")
    if congestion:
        outbound["congestion_control"] = congestion
    udp_mode = get_q("udp_relay_mode")
    if udp_mode:
        outbound["udp_relay_mode"] = udp_mode
    alpn = get_q("alpn")
    if alpn:
        # alpn приходит как список через запятую: alpn=h3,h2
        outbound["tls"]["alpn"] = [item for item in alpn.split(",") if item]
    return outbound


def parse_uri_to_singbox_outbound(uri, tag):
    """Преобразует VPN-URI в outbound-конфигурацию sing-box.

    Возвращает dict конфигурации или None, если URI не распознан.
    """
    try:
        parsed = urlparse(uri)
        # VMess хранит адрес и порт внутри Base64 JSON, а не в authority URI.
        # Shadowsocks тоже не проверяем здесь: форма ss://BASE64(method:pass@host:port)
        # даёт hostname=base64-тело (причём urlparse lowercases hostname и портит
        # base64), поэтому ss всегда разбираем в своей ветке по сырому uri.
        if parsed.scheme not in ("vmess", "ss") and (not parsed.hostname or not parsed.port):
            return None
        query = parse_qs(parsed.query)

        def get_q(key, default=""):
            return query.get(key, [default])[0]

        parsers = {
            "vless": lambda: _parse_vless(parsed, get_q, tag),
            "trojan": lambda: _parse_trojan(parsed, get_q, tag),
            "ss": lambda: _parse_ss(uri, parsed, tag),
            "vmess": lambda: _parse_vmess(uri, tag),
            "hysteria2": lambda: _parse_hysteria2(parsed, get_q, tag),
            "tuic": lambda: _parse_tuic(parsed, get_q, tag),
        }
        parser = parsers.get(parsed.scheme)
        return parser() if parser else None
    except Exception:
        return None
