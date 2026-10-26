"""Bounded preparation of non-seekable streams for multipart upload."""
import asyncio
import io
import tempfile


async def file_chunks(source, chunk_size=1024 * 1024):
    """Read a blocking stream without spilling or abandoning an in-flight read."""
    while True:
        worker = asyncio.create_task(asyncio.to_thread(source.read, chunk_size))
        try:
            chunk = await asyncio.shield(worker)
        except asyncio.CancelledError:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not worker.cancelled():
                worker.exception()
            raise
        if not chunk:
            return
        yield chunk.encode("utf-8") if isinstance(chunk, str) else chunk


async def rewindable_file(source):
    if isinstance(source, (bytes, bytearray)):
        return io.BytesIO(source)
    try:
        source.seek(0)
        return source
    except (AttributeError, OSError, io.UnsupportedOperation):
        pass
    target = tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024, mode="w+b")
    try:
        def copy_chunk():
            chunk = source.read(1024 * 1024)
            if chunk:
                target.write(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
            return bool(chunk)

        while True:
            # A blocking storage read must not starve task heartbeats. Join an
            # in-flight read before closing either stream after cancellation.
            worker = asyncio.create_task(asyncio.to_thread(copy_chunk))
            try:
                more = await asyncio.shield(worker)
            except asyncio.CancelledError:
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not worker.cancelled():
                    worker.exception()
                raise
            if not more:
                break
        target.seek(0)
        return target
    except BaseException:
        target.close()
        raise


def close_file(source):
    try:
        close = getattr(source, "close", None)
        if callable(close):
            close()
    finally:
        release = getattr(source, "release_conn", None)
        if callable(release):
            release()
