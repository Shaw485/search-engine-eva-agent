# Stage 0 portfolio deployment

This deployment exposes only the deterministic ten-product Stage 0 experience.
It does not deploy OpenSearch or claim formal ESCI evaluation quality.

## Topology

```text
shawspace.cn/search-eval.html
          |
          v
Nginx /search-eval-api/*
          |
          v
Uvicorn 127.0.0.1:8010
          |
          v
Local BM25 + deterministic-hash-v1 over data/samples/products.json
```

The API binds to loopback only. Nginx is the only public entry point, so the
portfolio page and API share the same HTTPS origin.

## First install

Run these commands on the server after reviewing the paths:

```bash
sudo git clone https://github.com/Shaw485/search-engine-eva-agent.git /var/www/search-engine-eva-agent
sudo python3 -m venv /var/www/search-engine-eva-agent/.venv
sudo /var/www/search-engine-eva-agent/.venv/bin/pip install -r /var/www/search-engine-eva-agent/requirements-dev.lock
sudo /var/www/search-engine-eva-agent/.venv/bin/pip install --no-build-isolation --no-deps -e /var/www/search-engine-eva-agent
sudo cp /var/www/search-engine-eva-agent/deploy/search-engine-eva-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now search-engine-eva-agent
```

Add `deploy/nginx-search-eval.conf` inside the existing HTTPS server block, then
validate and reload Nginx:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## Update and verify

```bash
sudo git -C /var/www/search-engine-eva-agent pull --ff-only
sudo /var/www/search-engine-eva-agent/.venv/bin/pip install --no-build-isolation --no-deps -e /var/www/search-engine-eva-agent
sudo systemctl restart search-engine-eva-agent
curl http://127.0.0.1:8010/health
curl 'https://shawspace.cn/search-eval-api/smoke?query=wireless%20mouse&top_k=3&backend=local'
```

Before a reload, `nginx -t` must pass. After an update, verify both the loopback
health endpoint and the public same-origin smoke endpoint.
