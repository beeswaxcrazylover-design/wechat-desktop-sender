"""Original application lock, scoped to the local runtime directory."""
from .simple_core import ForwardError


class AppLock:
    def __init__(self,path):self.path=path;self.handle=None
    def acquire(self):
        import msvcrt
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.handle=self.path.open('a+b')
        if self.path.stat().st_size==0:self.handle.write(b'0');self.handle.flush()
        self.handle.seek(0)
        try:msvcrt.locking(self.handle.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            self.handle.close();self.handle=None
            raise ForwardError('ALREADY_RUNNING') from None
    def close(self):
        if self.handle:self.handle.close();self.handle=None
