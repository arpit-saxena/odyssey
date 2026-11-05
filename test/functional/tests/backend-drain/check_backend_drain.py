#!/usr/bin/env python3
"""
A server connection that odyssey closes keeps its pool slot until the backend
has closed its side of the connection.

PostgreSQL releases a backend's max_connections slot only when the backend
process exits, which is also when it closes the socket.  If odyssey freed the
pool slot as soon as it had sent Terminate, a replacement connection could be
opened while the old backend was still exiting and be refused with "sorry,
too many clients already".

The test puts a proxy between odyssey and postgres.  The proxy forwards bytes
verbatim, except that it holds a Terminate message from odyssey for HOLD_S
seconds, which emulates a backend that is slow to exit.  The moment the
Terminate is seen, a second client runs a query through odyssey.  With
pool_size 1 that client needs a new server connection, and the proxy must not
see it before the old connection has been closed.

Both close paths are exercised:
  close  - the client leaves inside a transaction with pool_rollback off,
           so odyssey closes the server right away (od_router_close)
  expire - the server is returned to the pool and expired by pool_ttl 1
           (cron expire)
"""

import asyncio
import subprocess
import time
import unittest

PG_PORT = 5432
PROXY_PORT = 5439
ODY_PORT = 6432
HOLD_S = 1.0


class Proxy:
    def __init__(self):
        self.server = None
        self.conn_no = 0
        self.terminate_seen = asyncio.Event()
        self.terminate_forwarded_at = None
        self.old_closed_at = None
        self.new_connection_at = None

    async def start(self):
        self.server = await asyncio.start_server(
            self.handle, "127.0.0.1", PROXY_PORT
        )

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    def reset(self):
        self.terminate_seen = asyncio.Event()
        self.terminate_forwarded_at = None
        self.old_closed_at = None
        self.new_connection_at = None

    async def handle(self, odyssey_reader, odyssey_writer):
        self.conn_no += 1
        if self.terminate_seen.is_set() and self.new_connection_at is None:
            self.new_connection_at = time.monotonic()
        pg_reader, pg_writer = await asyncio.open_connection("127.0.0.1", PG_PORT)
        await asyncio.gather(
            self.pump(odyssey_reader, pg_writer, hold_terminate=True),
            self.pump(pg_reader, odyssey_writer, hold_terminate=False),
        )

    async def pump(self, reader, writer, hold_terminate):
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                if hold_terminate and data.endswith(b"X\x00\x00\x00\x04"):
                    self.terminate_seen.set()
                    await asyncio.sleep(HOLD_S)
                    self.terminate_forwarded_at = time.monotonic()
                writer.write(data)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            if hold_terminate and self.terminate_forwarded_at is not None:
                self.old_closed_at = time.monotonic()
            writer.close()


async def psql(*args):
    p = await asyncio.create_subprocess_exec(
        "psql", "-h", "127.0.0.1", "-p", str(ODY_PORT), "-U", "postgres",
        "-d", "postgres", "-At", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await p.communicate()
    return p.returncode, out.decode(), err.decode()


class BackendDrain(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.proxy = Proxy()
        await self.proxy.start()

    async def asyncTearDown(self):
        await self.proxy.stop()

    async def run_scenario(self, first_client_args):
        rc, out, err = await psql(*first_client_args)
        self.assertEqual(rc, 0, err)

        await asyncio.wait_for(self.proxy.terminate_seen.wait(), timeout=5)
        started = time.monotonic()

        rc, out, err = await psql("-c", "SELECT 42")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), "42")

        # let the old connection finish closing
        await asyncio.sleep(HOLD_S + 0.5)

        self.assertIsNotNone(self.proxy.terminate_forwarded_at)
        self.assertIsNotNone(self.proxy.new_connection_at,
                             "the second client did not open a new server connection")
        self.assertGreaterEqual(
            self.proxy.new_connection_at, self.proxy.terminate_forwarded_at,
            "a new server connection was opened before the old backend had "
            "closed its connection")
        self.assertGreaterEqual(
            self.proxy.new_connection_at - started, HOLD_S - 0.1,
            "the pool slot was released before the old backend went away")

    async def test_close(self):
        # BEGIN alone is handled by odyssey itself, the server is attached
        # on the first real statement; psql exits with the transaction open
        with open("/tmp/backend-drain-begin.sql", "w") as f:
            f.write("BEGIN;\nSELECT 1;\n")
        await self.run_scenario(["-f", "/tmp/backend-drain-begin.sql"])

    async def test_expire(self):
        self.proxy.reset()
        await self.run_scenario(["-c", "SELECT 1"])


if __name__ == "__main__":
    unittest.main()
