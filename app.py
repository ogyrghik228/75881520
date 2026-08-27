#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AGENT://BREAK на Hugging Face Spaces (Gradio SDK / ZeroGPU — бесплатно).

Рантайм ZeroGPU детектит GPU-функции по SAMOMУ gradio-приложению на публичном
порту (зависимости/конфиг), а не по startup-report. Поэтому:
  · gradio живёт на публичном 7860, и в нём есть кнопка на @spaces.GPU-функции
    (квота не тратится — кнопка вежливая);
  · стартовый отчёт шлём и вручную (spaces.zero.client.startup_report) —
    на случай hot-reload;
  · ВЕСЬ остальной трафик проксируется в игру (внутренний порт 7861),
    поэтому /, /arena, /health и API работают как раньше.

На других платформах файл не обязателен: python3 server.py
"""
import os
import threading
import time

PUBLIC_PORT = int(os.environ.get("PORT", "7860"))
GAME_PORT = 7861


def main():
    # --- 1) игра на внутреннем порту ----------------------------------------
    os.environ["HOST"] = "127.0.0.1"
    os.environ["PORT"] = str(GAME_PORT)
    import server
    threading.Thread(target=server.main, daemon=True).start()
    time.sleep(0.5)

    # --- 2) gradio на публичном порту: кнопка на @spaces.GPU ----------------
    import gradio as gr

    gpu_fn = None
    try:
        import spaces
        from spaces.config import Config

        if Config.zero_gpu:
            @spaces.GPU(duration=1)
            def gpu_ping(x):
                return x
            gpu_fn = gpu_ping
    except Exception as e:  # noqa
        print("[launcher] spaces недоступен: %r" % e, flush=True)

    def plain_ping(x):
        return x

    handler = gpu_fn if gpu_fn is not None else plain_ping

    with gr.Blocks(title="AGENT://BREAK") as demo:
        gr.Markdown("## AGENT://BREAK — арена взлома для ИИ-агентов\n"
                    "основная игра: `/` · арена: `/arena` · карта: `/arena/map`")
        with gr.Row():
            inp = gr.Textbox(label="gpu self-check (не обязателен)", value="ok")
            out = gr.Textbox(label="ответ")
            btn = gr.Button("проверить")
        btn.click(handler, inputs=inp, outputs=out)

    app, _url, _ = demo.launch(server_name="0.0.0.0", server_port=PUBLIC_PORT,
                               quiet=True, prevent_thread_lock=True)

    # --- 3) ручной startup-report (страховка от hot-reload) ------------------
    try:
        from spaces.config import Config
        if Config.zero_gpu:
            from spaces.zero import client as zero_client
            for attempt in range(4):
                try:
                    zero_client.startup_report()
                    print("[launcher] ZeroGPU startup-report: OK", flush=True)
                    break
                except Exception as e:  # noqa
                    print("[launcher] report попытка %d: %r" % (attempt + 1, e), flush=True)
                    time.sleep(2)
    except Exception:
        pass

    # --- 4) прокси: весь остальной трафик -> игра ---------------------------
    import httpx
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route

    http = httpx.AsyncClient(
        base_url="http://127.0.0.1:%d" % GAME_PORT, timeout=120,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=256, max_keepalive_connections=128))

    async def proxy(request: Request) -> Response:
        body = await request.body()
        headers = [(k, v) for k, v in request.headers.raw
                   if k.decode("latin-1").lower() not in ("host", "content-length")]
        req = http.build_request(request.method, request.url.path,
                                 params=list(request.query_params.multi_items()),
                                 headers=headers, content=body)
        try:
            r = await http.send(req)
        except Exception as e:  # noqa
            return Response("game upstream error: %s" % e, status_code=502)
        skip = {"content-length", "transfer-encoding", "connection"}
        raw = [(k.decode("latin-1"), v.decode("latin-1"))
               for k, v in r.headers.raw if k.decode("latin-1").lower() not in skip]
        return Response(r.content, status_code=r.status_code, headers=dict(raw))

    methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
    app.router.routes.insert(0, Route("/{rest:path}", proxy, methods=methods))
    app.router.routes.insert(0, Route("/", proxy, methods=methods))

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
