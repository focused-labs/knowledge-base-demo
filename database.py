import pandas as pd
import numpy as np
import openai
from llama_index.vector_stores import RedisVectorStore
from redis.commands.search.field import VectorField, TextField, NumericField
from llama_index import StorageContext, GPTVectorStoreIndex
from redis import Redis
from redis.commands.search.query import Query

from config import EMBEDDINGS_MODEL, PREFIX, VECTOR_FIELD_NAME, NOTION_INDEX_NAME, NOTION_PREFIX, WEB_SCRAPE_INDEX_NAME, \
    WEB_SCRAPE_PREFIX


def get_specific_index(index_name, prefix_name):
    storage_context = get_storage_context(index_name, prefix_name)
    index = GPTVectorStoreIndex([], storage_context=storage_context)
    return index


def get_index_set():
    notion_index = get_specific_index(NOTION_INDEX_NAME, NOTION_PREFIX)
    web_scrape_index = get_specific_index(WEB_SCRAPE_INDEX_NAME, WEB_SCRAPE_PREFIX)

    index_set = [notion_index, web_scrape_index]
    return index_set


def get_storage_context(index_name, prefix_name):
    vector_store = get_vector_store(index_name, prefix_name)
    return StorageContext.from_defaults(vector_store=vector_store)


def get_vector_store(index_name, prefix_name):
    return RedisVectorStore(
        index_name=index_name,
        index_prefix=prefix_name,
        redis_url="redis://localhost:6379",
        overwrite=True,
    )


# Get a Redis connection
def get_redis_connection(host='localhost', port='6379', db=0):
    r = Redis(host=host, port=port, db=db, decode_responses=False)
    return r


# Create a Redis index to hold our data
def create_hnsw_index(redis_conn, vector_field_name, vector_dimensions=1536, distance_metric='COSINE'):
    redis_conn.ft().create_index([
        VectorField(vector_field_name, "HNSW",
                    {"TYPE": "FLOAT32", "DIM": vector_dimensions, "DISTANCE_METRIC": distance_metric}),
        TextField("filename"),
        TextField("text_chunk"),
        NumericField("file_chunk_index")
    ])


# Create a Redis pipeline to load all the vectors and their metadata
def load_vectors(client: Redis, input_list, vector_field_name):
    p = client.pipeline(transaction=False)
    for text in input_list:
        # hash key
        key = f"{PREFIX}:{text['id']}"

        # hash values
        item_metadata = text['metadata']
        #
        item_keywords_vector = np.array(text['vector'], dtype='float32').tobytes()
        item_metadata[vector_field_name] = item_keywords_vector

        # HSET
        p.hset(key, mapping=item_metadata)

    p.execute()


# Make query to Redis
def query_redis(redis_conn, query, index_name, top_k=2):
    print("***** Querying Redis *****")
    print(f"Original Query: {query}")

    ## Creates embedding vector from user query
    embedded_query = np.array(openai.Embedding.create(
        input=query,
        model=EMBEDDINGS_MODEL,
    )["data"][0]['embedding'], dtype=np.float32).tobytes()
    print(f"Embedded Query: {embedded_query}")

    # prepare the query
    q = Query(f'*=>[KNN {top_k} @{VECTOR_FIELD_NAME} $vec_param AS vector_score]').sort_by('vector_score').paging(0,
                                                                                                                  top_k).return_fields(
        'vector_score', 'filename', 'text_chunk', 'text_chunk_index').dialect(2)
    params_dict = {"vec_param": embedded_query}

    # Execute the query
    results = redis_conn.ft(index_name).search(q, query_params=params_dict)
    print(f"Results: {results}")
    print("**************************")

    return results


# Get mapped documents from Weaviate results
def get_redis_results(redis_conn, query, index_name):
    # Get most relevant documents from Redis
    query_result = query_redis(redis_conn, query, index_name)

    # Extract info into a list
    query_result_list = []
    for i, result in enumerate(query_result.docs):
        result_order = i
        text = result.text_chunk
        score = result.vector_score
        query_result_list.append((result_order, text, score))

    # Display result as a DataFrame for ease of us
    result_df = pd.DataFrame(query_result_list)
    result_df.columns = ['id', 'result', 'certainty']
    return result_df
