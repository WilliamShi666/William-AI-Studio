from pymilvus import Collection, CollectionSchema, connections, utility

class MilvusClient:
    def __init__(self, host: str, port: str):
        connections.connect(alias="default", host=host, port=port)

    def create_collection(self, collection_name: str, fields: list):
        schema = CollectionSchema(fields=fields, description="Collection for storing data")
        if not utility.has_collection(collection_name):
            Collection(name=collection_name, schema=schema)

    def insert_data(self, collection_name: str, records: list):
        return Collection(collection_name).insert(records)

    def search(self, collection_name: str, query_vectors: list, top_k: int):
        search_params = {"metric_type": "L2", "params": {"nprobe": 10}}
        return Collection(collection_name).search(query_vectors, anns_field="embedding", param=search_params, limit=top_k)

    def delete_collection(self, collection_name: str):
        if utility.has_collection(collection_name):
            utility.drop_collection(collection_name)

    def close(self):
        connections.disconnect(alias="default")
