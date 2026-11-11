import subprocess

from Cython.Build import cythonize
from setuptools import Extension, setup

LIBS = ["libavcodec", "libavfilter", "libavformat", "libavutil"]


def pkg_config(flag):
    return subprocess.check_output(["pkg-config", flag, *LIBS], text=True).split()


link = pkg_config("--libs")

# library_dirs come before Python's own -L, which may hold another FFmpeg.
ext = Extension(
    "cymedia._core",
    sources=["src/cymedia/_core.pyx", "src/cymedia/engine.c", "src/cymedia/qsink.c",
             "src/cymedia/source.c"],
    include_dirs=["src/cymedia"],
    library_dirs=[a[2:] for a in link if a.startswith("-L")],
    libraries=[a[2:] for a in link if a.startswith("-l")],
    extra_compile_args=pkg_config("--cflags") + ["-std=c11", "-O2", "-Wall"],
    extra_link_args=[a for a in link if not a.startswith(("-L", "-l"))] + ["-lpthread"],
)

setup(ext_modules=cythonize([ext], compiler_directives={"language_level": 3}))
