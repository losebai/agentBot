"""Close the streaming iterator in its own task before releasing its runtime lease."""

import anyio
from starlette.responses import StreamingResponse


class ManagedStreamingResponse(StreamingResponse):
    async def stream_response(self, send) -> None:
        try:
            await super().stream_response(send)
        finally:
            # Starlette's disconnect task cancels the sender; cleanup must finish in that
            # same context so generator-owned ContextVar tokens and transactions are released.
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()
