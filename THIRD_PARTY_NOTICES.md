# Сторонние компоненты и лицензии

Anomizer — локальное приложение. Ниже перечислены компоненты, которые
попадают в готовые сборки. Полные тексты лицензий находятся в самих пакетах
(каталоги `*.dist-info/licenses/` в сборочном окружении) и на сайтах проектов.

| Компонент | Лицензия | Источник |
|---|---|---|
| Python | PSF License | https://www.python.org/ |
| PyInstaller | GPL-2.0-or-later с исключением для сборок | https://pyinstaller.org/ |
| FFmpeg 7.0.2 (Linux) / 7.1 (Windows), static | GPL-3.0-or-later | https://johnvansickle.com/ffmpeg/ , https://github.com/imageio/imageio-ffmpeg |
| OpenCV | Apache-2.0 | https://opencv.org/ |
| YuNet (`face_detection_yunet_2023mar.onnx`) | MIT, © 2020 Shiqi Yu | https://github.com/opencv/opencv_zoo/ |
| NumPy | BSD-3-Clause и другие | https://numpy.org/ |
| Pillow | MIT-CMU | https://python-pillow.org/ |
| imageio-ffmpeg (обёртка) | BSD-2-Clause | https://github.com/imageio/imageio-ffmpeg |
| FastAPI | MIT | https://fastapi.tiangolo.com/ |
| Starlette | BSD-3-Clause | https://www.starlette.io/ |
| Uvicorn | BSD-3-Clause | https://www.uvicorn.org/ |
| Pydantic / pydantic-core | MIT | https://docs.pydantic.dev/ |
| AnyIO | MIT | https://anyio.readthedocs.io/ |
| h11 | MIT | https://github.com/python-hyper/h11 |
| Click | BSD-3-Clause | https://click.palletsprojects.com/ |

## FFmpeg

В сборку входит статический бинарник FFmpeg, собранный с `--enable-gpl
--enable-version3` и содержащий компоненты под GPL. Linux-сборка использует
FFmpeg 7.0.2 с johnvansickle.com, Windows-сборка — FFmpeg 7.1 из пакета
imageio-ffmpeg. Оба бинарника распространяются на условиях GNU GPL v3 (или
позднее). Исходный код FFmpeg и скрипты сборки доступны на
https://ffmpeg.org/download.html, https://johnvansickle.com/ffmpeg/ и
https://github.com/imageio/imageio-ffmpeg. Полный текст GPL v3:
https://www.gnu.org/licenses/gpl-3.0.txt.

FFmpeg запускается как отдельный процесс; приложение не компонует его
библиотеки в собственный исполняемый файл. Если вы распространяете сборку
дальше, приложите этот файл и текст соответствующей лицензии.

## YuNet

Модель распространяется по MIT License; текст приведён в
`models/README.md`, который включён в каждую сборку.
