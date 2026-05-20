
import json
import math
import os
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen


def _parse_texto(texto: str):

    linhas = (texto or "").splitlines()

    validos = []

    for linha in linhas:

        linha = linha.strip()

        if not linha:
            continue

        if ":" in linha:

            autor, conteudo = linha.split(":", 1)

            autor = autor.strip().lstrip("@").lower()

            conteudo = conteudo.strip()

            if not autor:
                continue

            if not re.search(r"@[A-Za-z0-9_.]+", conteudo):
                continue

            validos.append(autor)

            continue

        if linha.startswith("@"):

            usuario = linha.split(" ", 1)[0].strip().lstrip("@").lower()

            if not usuario:
                continue

            validos.append(usuario)

    contagem = {}

    for user in validos:
        contagem[user] = contagem.get(user, 0) + 1

    return validos, contagem


def _peso(qtd):

    return 1.0 + min(qtd * 0.35, 8.0)


def _build_pool(contagem):

    participantes = list(contagem.keys())

    pesos = {
        user: _peso(contagem[user])
        for user in participantes
    }

    pool = []

    for user in participantes:

        repeticoes = int(math.floor(pesos[user] * 10))

        if repeticoes < 1:
            repeticoes = 1

        pool.extend([user] * repeticoes)

    return pool, pesos


def _normalize_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    parsed = parsed._replace(query="", fragment="")
    return urlunparse(parsed).rstrip("/")


def _http_get_json(url: str, timeout_seconds: int = 30) -> dict:
    req = Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read()
    except HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except Exception:
            payload = {}
        err = payload.get("error") or {}
        message = err.get("message") or f"HTTP {e.code}"
        raise RuntimeError(message) from e
    except URLError as e:
        raise RuntimeError(f"Falha de rede: {e}") from e

    try:
        payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except Exception as e:
        raise RuntimeError("Resposta inválida da API") from e

    if isinstance(payload, dict) and "error" in payload:
        err = payload.get("error") or {}
        message = err.get("message") or "Erro desconhecido"
        raise RuntimeError(message)

    if not isinstance(payload, dict):
        raise RuntimeError("Resposta inválida da API")

    return payload


def _graph_get(path: str, params: dict[str, Any]) -> dict:
    token = os.environ.get("IG_GRAPH_ACCESS_TOKEN") or os.environ.get("FB_ACCESS_TOKEN")
    if not token:
        raise RuntimeError(
            "Faltou IG_GRAPH_ACCESS_TOKEN no servidor.\n"
            "No PowerShell:\n"
            "$env:IG_GRAPH_ACCESS_TOKEN=\"SEU_TOKEN\""
        )

    version = os.environ.get("IG_GRAPH_VERSION") or "v25.0"
    params2 = dict(params)
    params2["access_token"] = token
    url = f"https://graph.facebook.com/{version}/{path.lstrip('/')}" + "?" + urlencode(params2)
    return _http_get_json(url, timeout_seconds=30)


def _resolve_media_id_by_permalink(ig_user_id: str, post_url: str, search_pages: int = 10) -> str:
    target = _normalize_url(post_url)
    after: Optional[str] = None

    for _ in range(search_pages):
        params: dict[str, Any] = {"fields": "id,permalink", "limit": 100}
        if after:
            params["after"] = after

        payload = _graph_get(f"{ig_user_id}/media", params)
        data = payload.get("data") or []
        for item in data:
            permalink = _normalize_url(str(item.get("permalink") or ""))
            if permalink and permalink == target:
                media_id = item.get("id")
                if media_id:
                    return str(media_id)

        paging = payload.get("paging") or {}
        cursors = paging.get("cursors") or {}
        after = cursors.get("after")
        if not after:
            break

    raise RuntimeError(
        "Não achei esse post no /media do IG_USER_ID.\n"
        "A API oficial só consegue ler comentários de mídia da própria conta conectada ao IG_USER_ID."
    )


def _fetch_comments_instagram(post_url: str, limit: int) -> list[dict]:
    ig_user_id = os.environ.get("IG_USER_ID")
    if not ig_user_id:
        raise RuntimeError(
            "Faltou IG_USER_ID no servidor.\n"
            "No PowerShell:\n"
            "$env:IG_USER_ID=\"SEU_IG_USER_ID\""
        )

    media_id = _resolve_media_id_by_permalink(ig_user_id, post_url)

    out: list[dict] = []
    after: Optional[str] = None

    while len(out) < limit:
        batch_limit = min(50, max(1, limit - len(out)))
        params: dict[str, Any] = {"fields": "id,text,timestamp,username", "limit": batch_limit}
        if after:
            params["after"] = after

        payload = _graph_get(f"{media_id}/comments", params)
        data = payload.get("data") or []
        for item in data:
            username = item.get("username") or ""
            text = item.get("text") or ""
            if not username:
                continue
            out.append({"usuario": str(username), "comentario": str(text)})
            if len(out) >= limit:
                break

        paging = payload.get("paging") or {}
        cursors = paging.get("cursors") or {}
        after = cursors.get("after")
        if not after:
            break

    return out


class Handler(BaseHTTPRequestHandler):

    server_version = "SorteadorHTTP/1.0"

    def log_message(self, format, *args):
        return

    def _send_json(self, status, payload):

        body = json.dumps(
            payload,
            ensure_ascii=False
        ).encode("utf-8")

        self.send_response(status)

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type"
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET,POST,OPTIONS"
        )

        self.end_headers()

        self.wfile.write(body)

    def _send_bytes(self, status, body, content_type):

        self.send_response(status)

        self.send_header(
            "Content-Type",
            content_type
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.end_headers()

        self.wfile.write(body)

    def do_OPTIONS(self):

        self.send_response(
            HTTPStatus.NO_CONTENT
        )

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type"
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET,POST,OPTIONS"
        )

        self.end_headers()

    def do_GET(self):

        if self.path in ("/", "/index.html"):

            root_dir = os.path.dirname(
                os.path.abspath(__file__)
            )

            index_path = os.path.join(
                root_dir,
                "index.html"
            )

            try:

                with open(index_path, "rb") as f:

                    body = f.read()

            except FileNotFoundError:

                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {
                        "error": "index.html não encontrado"
                    }
                )

                return

            self._send_bytes(
                HTTPStatus.OK,
                body,
                "text/html; charset=utf-8"
            )

            return

        if self.path == "/api/health":

            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True
                }
            )

            return

        self._send_json(
            HTTPStatus.NOT_FOUND,
            {
                "error": "rota não encontrada"
            }
        )

    def do_POST(self):

        if self.path not in (
            "/api/draw",
            "/api/parse",
            "/api/instagram/fetch"
        ):

            self._send_json(
                HTTPStatus.NOT_FOUND,
                {
                    "error": "rota não encontrada"
                }
            )

            return

        try:

            content_length = int(
                self.headers.get(
                    "Content-Length",
                    "0"
                )
            )

        except ValueError:

            content_length = 0

        raw = (
            self.rfile.read(content_length)
            if content_length > 0
            else b""
        )

        try:

            data = json.loads(
                raw.decode("utf-8") or "{}"
            )

        except json.JSONDecodeError:

            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "JSON inválido"
                }
            )

            return

        if self.path == "/api/instagram/fetch":

            post_url = str(
                data.get("postUrl") or ""
            )

            try:

                limit = int(
                    data.get("limit", 5000)
                )

            except:

                limit = 5000

            if limit < 1:
                limit = 1

            if limit > 50000:
                limit = 50000

            try:

                comentarios = _fetch_comments_instagram(
                    post_url,
                    limit
                )

            except Exception as e:

                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "error": f"Erro ao puxar comentários: {e}"
                    }
                )

                return

            texto = "\n".join([
                f"{c['usuario']}:{str(c['comentario']).replace(chr(10), ' ').strip()}"
                for c in comentarios
            ])

            validos, contagem = _parse_texto(texto)

            participantes = list(contagem.keys())

            self._send_json(
                HTTPStatus.OK,
                {
                    "texto": texto,
                    "totalComentarios": len(validos),
                    "participantesUnicos": len(participantes),
                    "contagem": contagem,
                },
            )

            return

        texto = str(data.get("texto", ""))

        validos, contagem = _parse_texto(texto)

        participantes = list(contagem.keys())

        if self.path == "/api/parse":

            self._send_json(
                HTTPStatus.OK,
                {
                    "totalComentarios": len(validos),
                    "participantesUnicos": len(participantes),
                    "contagem": contagem,
                },
            )

            return

        if len(participantes) == 0:

            self._send_json(
                HTTPStatus.OK,
                {
                    "totalComentarios": 0,
                    "participantesUnicos": 0,
                    "contagem": {},
                    "vencedor": None,
                    "logs": [],
                    "pesos": {},
                },
            )

            return

        pool, pesos = _build_pool(contagem)

        vencedor = secrets.choice(pool)

        logs = [

            {
                "user": user,
                "qtd": contagem[user],
                "peso": round(pesos[user], 2),
                "vencedor": user == vencedor
            }

            for user in sorted(
                contagem.keys(),
                key=lambda u: contagem[u],
                reverse=True
            )
        ]

        self._send_json(
            HTTPStatus.OK,
            {
                "totalComentarios": len(validos),
                "participantesUnicos": len(participantes),
                "contagem": contagem,
                "vencedor": vencedor,
                "logs": logs,
                "pesos": pesos,
            },
        )


def main():

    port = int(
        os.environ.get("PORT", "8000")
    )

    server = ThreadingHTTPServer(
        ("0.0.0.0", port),
        Handler
    )

    print(
        f"Servidor rodando: http://localhost:{port}/"
    )

    server.serve_forever()


if __name__ == "__main__":

    main()

