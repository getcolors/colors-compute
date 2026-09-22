import asyncio
import sys
import pytest
from colors_compute.ssh import _process_closer


@pytest.mark.asyncio
async def test_repeated_cancellation_drains_cleanup_and_repeated_close_is_idempotent():
    child = await asyncio.create_subprocess_exec(
        sys.executable, '-c', 'import sys,time; sys.stdin.read(); time.sleep(.08)',
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL)
    cleaned = []
    close = _process_closer(child, lambda: cleaned.append('done'))
    task = asyncio.create_task(close())
    await asyncio.sleep(.01)
    task.cancel()
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.returncode == 0
    assert cleaned == ['done']
    await asyncio.gather(close(), close())
    assert cleaned == ['done']
