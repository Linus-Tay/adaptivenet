# This Mac's IPv6 route to AWS black-holes; force IPv4 for the live telemetry tools.
import socket
_orig = socket.getaddrinfo
socket.getaddrinfo = lambda h, p, family=0, *a, **k: _orig(h, p, socket.AF_INET, *a, **k)
