// Tiny static file server for the cache pod. Serves only files directly inside /srv, and only
// once the tarball and its checksum are complete (the READY marker exists).
const http = require('http');
const fs = require('fs');
const path = require('path');

const root = '/srv';
const port = Number(process.env.PORT) || 8081;
const ready = () => fs.existsSync(path.join(root, 'READY'));

http
  .createServer((req, res) => {
    const url = decodeURIComponent(req.url.split('?')[0]);
    if (url === '/healthz') {
      res.writeHead(ready() ? 200 : 503, { 'Content-Type': 'text/plain' });
      return res.end(ready() ? 'ok' : 'installing');
    }
    const file = path.join(root, path.basename(url)); // basename: no path traversal
    if (!ready() || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
      res.writeHead(404);
      return res.end();
    }
    res.writeHead(200, { 'Content-Length': fs.statSync(file).size });
    fs.createReadStream(file).pipe(res);
  })
  .listen(port, () => console.log(`cache serving on :${port}`));
