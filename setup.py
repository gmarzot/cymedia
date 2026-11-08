import subprocess

from Cython.Build import cythonize
from setuptools import Extension, setup

LIBS = ["libavcodec", "libavfilter", "libavutil"]


def pkg_config(flag):
    return subprocess.check_output(["pkg-config", flag, *LIBS], text=True).split()


ext = Extension(
    "cymedia._core",
    sources=["src/cymedia/_core.pyx", "src/cymedia/engine.c", "src/cymedia/qsink.c"],
    include_dirs=["src/cymedia"],
    extra_compile_args=pkg_config("--cflags") + ["-std=c11", "-O2", "-Wall"],
    extra_link_args=pkg_config("--libs") + ["-lpthread"],
)

setup(ext_modules=cythonize([ext], compiler_directives={"language_level": 3}))
