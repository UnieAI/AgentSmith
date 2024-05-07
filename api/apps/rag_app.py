from flask import request
from flask_login import login_required
from api.db.services.dialog_service import DialogService, use_retrival
from api.utils.api_utils import server_error_response, get_data_error_result, validate_request
from api.utils.api_utils import get_json_result
from rag.utils import ELASTICSEARCH
from api.settings import RetCode

from langchain_community.embeddings import HuggingFaceInstructEmbeddings
from search_algorithm.rrf import rrf

hardCodeEmbeddings = HuggingFaceInstructEmbeddings(model_name="GanymedeNil/text2vec-large-chinese")

def search(index, kb_ids, query, size = 10, from_ = 0, hightlight = False):
    if not es.indices.exists(index=index):
        return get_data_error_result(retmsg=f"Index with name: {index} does not exist")
        
    match_res = es.search(
                index=index,
                body={
                    "query": {
                        "bool": {
                            "must": [
                                {"match": {"text": query}}
                            ],
                            "filter": [
                                {"terms": {"id": kb_ids}}
                            ]
                        }
                    },
                    "from": from_,
                    "size": size,
                    "highlight": {
                        "fields": {"text": {}},
                        "pre_tags": "<mark>",
                        "post_tags": "</mark>",
                    }
                }
            )
    
    knn_res = es.search(
                index=index,
                body={
                    "query": {
                        "bool": {
                            "must": [
                                {
                                    "knn": {
                                        "field": "vector",
                                        "query_vector": hardCodeEmbeddings.embed_query(query),
                                        "k": 10,
                                        "num_candidates": 50,
                                        "boost": 0.1
                                    }
                                }
                            ],
                            "filter": [
                                {"terms": {"id": kb_ids}}
                            ]
                        }
                    },
                    "from": from_,
                    "size": size
                }
            )

    merged_results = rrf(match_res, knn_res)
    return merged_results

@manager.route('', methods=['POST'])
# TODO: Add login_required decorator
# @login_required
@validate_request("agent", "messages")
def rag():
    req = request.json
    agent = req["agent"]
    messages = req["messages"]

    system = None
    query = None

    for m in req["messages"]:
        if m["role"] == "system":
            system = m["content"]
            continue
        if m["role"] == "assistant" and not msg:
            continue
        query = m["content"]

    try:
        e, dia = DialogService.get_by_name(agent)
        if not e:
            return get_data_error_result(retmsg="Dialog not found!")
            
        ref = search(dialog.tenant_id, dialog.kb_ids, query)

        return get_json_result(data=ref)
    except Exception as e:
        return server_error_response(e)
