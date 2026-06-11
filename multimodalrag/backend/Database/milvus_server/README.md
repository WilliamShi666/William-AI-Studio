# Roys Legion Milvus Stack

This directory contains the local Milvus stack used by Roys Legion.

## Services

- `etcd`
- `minio`
- `standalone` Milvus
- `attu` Milvus UI

The default compose file uses public upstream images and a CPU-friendly Milvus image.

## Local Start

```bash
cp .env.example .env
docker compose up -d
```

Or:

```bash
./start_milvus.sh
```

## Local-Only Defaults

The default MinIO credentials are local demo values:

```text
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=minioadmin
```

Override them in `.env` for any shared, remote, or public deployment.

## Data Volumes

Runtime state is stored under:

```text
data/volumes/
```

These volumes are excluded from the normal public source export. Rebuild Milvus from the Roys Legion Demo Dataset instead of committing raw Milvus/etcd/MinIO volume state.

## Public Dataset Import

The canonical public data path is:

```text
multimodalrag/datasets/public_tutoring_demo/
```

Use its manifest and import scripts to populate a local Milvus collection.
