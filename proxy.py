import asyncio
import re
import socket

DAPHNE_PORT = 8002
GO2RTC_PORT = 1984

def configure_socket(writer):
    try:
        sock = writer.get_extra_info('socket')
        if sock:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
    except Exception:
        pass

async def handle_client(reader, writer):
    try:
        client_addr = writer.get_extra_info('peername')
        configure_socket(writer)
        # Read the initial HTTP request data
        data = await reader.read(8192)
        if not data:
            writer.close()
            return
            
        # Parse the HTTP request line
        first_line = data.split(b'\r\n')[0].decode('utf-8', errors='ignore')
        method, path, proto = first_line.split(' ') if len(first_line.split(' ')) == 3 else ("GET", "/", "HTTP/1.1")
        
        print(f"[PROXY] {client_addr} -> {method} {path}")
        
        target_port = DAPHNE_PORT
        target_host = "127.0.0.1"
        
        # Rewrite URL if needed and set target port
        if path.startswith("/go2rtc/"):
            target_port = GO2RTC_PORT
            new_path = path[7:] # remove /go2rtc
            if not new_path.startswith("/"):
                new_path = "/" + new_path
            # Replace path in the raw HTTP request
            data = data.replace(path.encode('utf-8'), new_path.encode('utf-8'), 1)
        elif path.startswith("/ws/go2rtc/"):
            target_port = GO2RTC_PORT
            # Map /ws/go2rtc/camera_20/ -> /api/ws?src=camera_20
            match = re.match(r"^/ws/go2rtc/([\w\-]+)/?$", path)
            if match:
                stream_name = match.group(1)
                new_path = f"/api/ws?src={stream_name}"
                data = data.replace(path.encode('utf-8'), new_path.encode('utf-8'), 1)

        # Connect to upstream
        upstream_reader, upstream_writer = await asyncio.open_connection(target_host, target_port)
        configure_socket(upstream_writer)
        
        # Send the initial buffer
        upstream_writer.write(data)
        await upstream_writer.drain()
        
        # Pipe the streams
        async def forward_data(r, w):
            try:
                while True:
                    chunk = await r.read(65536)
                    if not chunk:
                        break
                    w.write(chunk)
                    await w.drain()
            except Exception:
                pass
            finally:
                try:
                    if w.can_write_eof():
                        w.write_eof()
                except Exception:
                    pass

        # Run bidirectional piping
        t1 = asyncio.create_task(forward_data(reader, upstream_writer))
        t2 = asyncio.create_task(forward_data(upstream_reader, writer))

        # The response stream (t2) defines the request lifetime.
        # If client disconnects, writing in t2 will fail or t1 will fail.
        await t2
        t1.cancel()
    except Exception as e:
        pass
    finally:
        try:
            upstream_writer.close()
        except Exception:
            pass
        try:
            writer.close()
        except Exception:
            pass

async def main():
    while True:
        try:
            server = await asyncio.start_server(handle_client, '0.0.0.0', 8000, reuse_address=True)
            print("[PROXY] Listening on 0.0.0.0:8000 -> Forwarding to Daphne (8002) and Go2RTC (1984)", flush=True)
            async with server:
                await server.serve_forever()
        except OSError as e:
            print(f"[PROXY] Bind error: {e}. Retrying in 2 seconds...", flush=True)
            await asyncio.sleep(2)
        except Exception as e:
            print(f"[PROXY] Unexpected error: {e}. Retrying in 2 seconds...", flush=True)
            await asyncio.sleep(2)

if __name__ == '__main__':
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass
    asyncio.run(main())

