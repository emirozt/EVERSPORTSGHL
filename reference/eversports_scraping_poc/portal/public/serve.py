import http.server, os, sys
os.chdir(os.path.dirname(os.path.abspath(__file__)))
httpd = http.server.HTTPServer(('', 4321), http.server.SimpleHTTPRequestHandler)
httpd.serve_forever()
