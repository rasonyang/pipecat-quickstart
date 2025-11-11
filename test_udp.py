#!/usr/bin/env python3
"""Simple UDP echo server to test if we can receive UDP packets."""

import asyncio
import socket


class TestUDPProtocol(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport
        print(f"✅ UDP protocol ready")

    def datagram_received(self, data, addr):
        print(f"🔵 Received {len(data)} bytes from {addr}")
        print(f"   Data: {data[:100]}")
        # Echo back
        self.transport.sendto(b"RECEIVED", addr)


async def main():
    host = "172.16.204.89"
    port = 6060

    print(f"Starting UDP test server on {host}:{port}")

    loop = asyncio.get_event_loop()

    # Create socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setblocking(False)

    try:
        sock.bind((host, port))
        print(f"✅ Socket bound to {sock.getsockname()}")
    except Exception as e:
        print(f"❌ Failed to bind: {e}")
        return

    # Create protocol
    transport, protocol = await loop.create_datagram_endpoint(
        TestUDPProtocol,
        sock=sock
    )

    print(f"✅ Listening for UDP packets...")
    print(f"   Send a test packet with: echo 'TEST' | nc -u {host} {port}")

    try:
        # Run forever
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        transport.close()


if __name__ == "__main__":
    asyncio.run(main())
