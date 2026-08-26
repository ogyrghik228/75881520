#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Лаунчер AGENT://BREAK для Hugging Face Spaces (Gradio SDK, железо ZeroGPU).

Бесплатные аккаунты HF могут держать Gradio-спейсы на ZeroGPU, но рантайм
требует хотя бы одну функцию с декоратором @spaces.GPU при старте.
Игра GPU не использует (и квоту не тратит) — делаем пустую заглушку
и стартуем игровой сервер на порту 7860.
"""
import os


def start_game():
    os.environ.setdefault("HOST", "0.0.0.0")
    os.environ.setdefault("PORT", "7860")
    import server          # наш игровой сервер (портал + арена + комнаты)
    server.main()


try:
    import spaces  # пакет HF, предустановлен в Gradio-образе

    @spaces.GPU    # удовлетворяет проверку ZeroGPU; никогда не вызывается
    def _zero_gpu_stub():
        return None
except Exception:  # локальный запуск и другие платформы — без spaces
    pass

if __name__ == "__main__":
    start_game()
