# flight-serve, the tape reader, as an MCP server over stdio:
#     docker run -i -v /path/to/flight:/data/flight flight-recorder
# The tapes are read from /data/flight; mount the directory your app records into. Read-only:
# the server never writes there. With nothing mounted it starts and lists no tapes.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY flight_recorder ./flight_recorder
COPY flight_sink ./flight_sink
RUN pip install --no-cache-dir ".[serve]"

WORKDIR /data
ENTRYPOINT ["flight-serve", "/data/flight"]
