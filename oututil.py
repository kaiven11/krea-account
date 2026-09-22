import sys
import io


def setup_utf8_stdout():
    if getattr(sys.stdout, "_utf8_wrapped", False):
        return
    wrapper = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    wrapper._utf8_wrapped = True
    sys.stdout = wrapper
    if not getattr(sys.stderr, "_utf8_wrapped", False):
        err = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        err._utf8_wrapped = True
        sys.stderr = err
