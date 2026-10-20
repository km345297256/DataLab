# DataLab Frontend

Vue 3, TypeScript, Vite, and Tailwind frontend for DataLab.

## Retained pages

- `/dataset/seek/:datasetId` — scientific dataset exploration and analysis.
- `/chat` and `/chat/:sessionId` — general data exploration tasks.
- `/chat/plugins` — Skill, MCP, and Renderer plugins.
- `/chat/admin` — resource usage, tasks, MCP, and skills only.

## Development and deployment

```bash
cd ..
./run.sh up -d --build
```

The frontend is built and deployed with DataLab's isolated Compose stack and is available at `http://localhost:10030` by default. Use `./run.sh` from the repository root for container updates.

## Checks

```bash
npm run type-check
npm run build
node --test tests/*.test.mjs
```

## Docker

```bash
docker build -t datalab-frontend .
docker run --rm -p 10030:80 -e BACKEND_URL=http://backend:8000 datalab-frontend
```

For a complete deployment, use `./run.sh` with the root `docker-compose.yml` and the isolated defaults in `deploy/datalab/compose.env.example`.
