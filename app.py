#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Лаунчер для Hugging Face Spaces (SDK: Gradio, CPU basic · FREE).

HF-спейс с SDK «gradio» просто запускает app.py и ждёт HTTP на порту 7860 —
что за сервер, ему без разницы. Поэтому стартуем игру прямо здесь.
"""
import os


def main():
    os.environ.setdefault("HOST", "0.0.0.0")
    os.environ.setdefault("PORT", "7860")
    import server          # наш игровой сервер (портал + арена + комнаты)
    server.main()


if __name__ == "__main__":
    main()
