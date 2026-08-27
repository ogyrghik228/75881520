#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AGENT://BREAK на Hugging Face Spaces (Gradio SDK / ZeroGPU — бесплатно).

Что требует рантайм ZeroGPU: зарегистрированную @spaces.GPU-функцию и
startup-report на локальный control-API. Обычно отчёт шлёт пакет spaces
при первом gradio-запуске (патч gr.Blocks.launch), НО в hot-reload режиме
gradio 6 этот путь не срабатывает — поэтому шлём отчёт вручную.

Игра стартует напрямую на публичном порту 7860. На других платформах этот
файл не обязателен: там сервер запускается как `python3 server.py`.
"""
import os
import threading
import time

PUBLIC_PORT = int(os.environ.get("PORT", "7860"))


def main():
    # --- 1) игра напрямую на публичном порту --------------------------------
    os.environ["HOST"] = "0.0.0.0"
    os.environ["PORT"] = str(PUBLIC_PORT)
    import server
    threading.Thread(target=server.main, daemon=True).start()
    time.sleep(0.5)

    # --- 2) ZeroGPU: заглушка + ручной startup-report -----------------------
    try:
        import spaces
        from spaces.config import Config

        if Config.zero_gpu:
            @spaces.GPU              # требование рантайма; никогда не вызывается
            def _zero_gpu_stub():
                return None

            from spaces.zero import client as zero_client
            for attempt in range(4):
                try:
                    zero_client.startup_report()
                    print("[launcher] ZeroGPU startup-report: OK", flush=True)
                    break
                except Exception as e:  # noqa
                    print("[launcher] report попытка %d не прошла: %r" % (attempt + 1, e),
                          flush=True)
                    time.sleep(2)
        else:
            print("[launcher] не ZeroGPU-рантайм — отчёт не нужен", flush=True)
    except Exception as e:  # noqa
        print("[launcher] spaces недоступен (ок для не-HF платформ): %r" % e, flush=True)

    # --- 3) gradio-стаб (не обязателен, но пусть будет запасным путём) ------
    try:
        import gradio as gr
        with gr.Blocks(title="AGENT://BREAK") as demo:
            gr.Markdown("## AGENT://BREAK")
        demo.launch(server_name="127.0.0.1", server_port=7862,
                    quiet=True, prevent_thread_lock=True)
    except Exception as e:  # noqa
        print("[launcher] gradio-стаб не поднялся (не страшно): %r" % e, flush=True)

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
