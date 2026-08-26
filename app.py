#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AGENT://BREAK на Hugging Face Spaces (Gradio SDK / ZeroGPU — бесплатно).

Рантайм ZeroGPU ждёт от приложения запуск gradio (demo.launch()) и хотя бы
одну @spaces.GPU-функцию — тогда пакет spaces отправляет startup-report
и спейс не убивают. КАКОЙ порт слушает gradio — рантайму не важно.
Поэтому:
  · игра стартует напрямую на публичном порту 7860 (без прокси — race-уровень
    и параллельные атаки работают нативно);
  · крошечный Gradio поднимается на тихом 127.0.0.1:7862 чисто ради launch().

На других платформах (локально, Docker) этот файл не обязателен — там
сервер запускается напрямую: python3 server.py
"""
import os
import threading
import time

PUBLIC_PORT = int(os.environ.get("PORT", "7860"))


def main():
    # --- 1) игра напрямую на публичном порту (в отдельном потоке) ---------
    os.environ["HOST"] = "0.0.0.0"
    os.environ["PORT"] = str(PUBLIC_PORT)
    import server
    game = threading.Thread(target=server.main, daemon=True)
    game.start()
    time.sleep(0.5)

    # --- 2) gradio на тихом порту: только чтобыspaces отправил отчёт ------
    try:
        import spaces

        @spaces.GPU          # требование рантайма; никогда не вызывается
        def _zero_gpu_stub():
            return None
    except Exception:
        pass                  # локальный запуск / не-HF платформы

    try:
        import gradio as gr
        with gr.Blocks(title="AGENT://BREAK") as demo:
            gr.Markdown("## AGENT://BREAK\n\nосновной сервис — на порту %d" % PUBLIC_PORT)
        demo.launch(server_name="127.0.0.1", server_port=7862,
                    quiet=True, prevent_thread_lock=True)
    except Exception as e:    # noqa — gradio не критичен для игры
        print("[launcher] gradio-стаб не поднялся (не страшно): %r" % e, flush=True)

    game.join()               # живём, пока живёт игра


if __name__ == "__main__":
    main()
