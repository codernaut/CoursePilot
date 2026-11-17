# CoursePilot on Kubernetes

Plain manifests (no Helm) for one CoursePilot instance. Optional: nothing else in the project needs Kubernetes or Docker. See also `docs/deploy.md` guide F and the user manual chapter "Deployment options and system requirements".

## Why exactly one replica, and why a worker sidecar

- CoursePilot keeps its data in **SQLite** (one writer) and throttles logins in memory, so it runs as **one copy**. `replicas: 1` and `strategy: Recreate` are deliberate: a rolling update would briefly run two copies on the same database. A short downtime during an upgrade is expected.
- The **web** and the **worker** share the same data volume. A `ReadWriteOnce` volume can be used by pods on one node only, so both run as two containers in **one pod** instead of two Deployments.
- The volume must be block storage (cloud disk, local SSD, Longhorn, Ceph RBD...), never NFS/SMB/EFS.

## Steps

1. **Build and push the image** (from the repository root) to a registry your cluster can pull from:
   ```bash
   docker build -t registry.example.com/coursepilot:v0.1.0 --build-arg GIT_COMMIT=$(git rev-parse HEAD) .
   docker push registry.example.com/coursepilot:v0.1.0
   ```
   Then set that name in `kustomization.yaml` (`images:`), or edit `image:` in `deployment.yaml`.
2. **Edit** `ingress.yaml` (host, ingress class, TLS) and `pvc.yaml` (size, storage class).
3. **Create the settings secret** from a `.env` file (copy `.env.example`; for the cluster set `APP_ENV=production`, `BASE_URL=https://<your host>`, a `SECRET_KEY` of 32+ random characters, `BEHIND_PROXY=true`, and your API keys). Values must **not** be quoted in this file, and it must not contain inline comments:
   ```bash
   kubectl apply -f deploy/kubernetes/namespace.yaml
   kubectl create secret generic coursepilot-env -n coursepilot --from-env-file=.env
   ```
   `DATA_DIR`, `DATABASE_URL`, `CHECKPOINT_DB_PATH` and `APP_HOST` are set by the Deployment and override the secret.
4. **Apply**: `kubectl apply -k deploy/kubernetes` (the web container migrates the database on start).
5. **First admin and structure**:
   ```bash
   kubectl exec -it -n coursepilot deploy/coursepilot -c web -- python -m app.cli create-admin
   kubectl exec -n coursepilot deploy/coursepilot -c web -- python -m app.cli seed-structure
   kubectl exec -n coursepilot deploy/coursepilot -c web -- python -m app.cli check --production
   ```
6. Open `https://<your host>`.

## Backups

`kubectl exec -n coursepilot deploy/coursepilot -c web -- python -m app.cli backup --out /data/backups --keep 7`, then copy the folder off the cluster (`kubectl cp`), and/or snapshot the volume. The backup does not include the secret: keep your `.env` safe. To restore, scale to zero (`kubectl scale deploy/coursepilot -n coursepilot --replicas=0`), run `restore` in a one-off pod that mounts the claim, and scale back to 1.

## Upgrading

Build and push a new tag, change `newTag` in `kustomization.yaml`, run `kubectl apply -k deploy/kubernetes`. Back up first. Migrations run when the new web container starts. After changing `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL` or `EMBEDDING_DIM`, run `python -m app.cli reindex-all` the same way as step 5.

## Notes

- The root file system is read-only; only `/data` (the volume) and `/tmp` are writable. If your image or a future version needs more, remove `readOnlyRootFilesystem` from both containers.
- Resources match the manual's sizing (4 CPU / 8 GB node recommended); the worker's memory peaks when parsing very large PDFs.
- Probes: readiness `/api/v1/health/ready`, liveness `/api/v1/health/live`.
- OCR is not in the image (`OCR_ENABLED` is off); see the comment at the top of the `Dockerfile`.
