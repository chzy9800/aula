"""``python -m aula`` 等价于安装后的 ``aula``。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
