from flask import request
from flask_login import login_required
from api.db.services.dialog_service import DialogService, use_retrival
from api.utils.api_utils import server_error_response, get_data_error_result, validate_request
from api.utils.api_utils import get_json_result
from rag.utils import ELASTICSEARCH
from api.settings import RetCode

import numpy as np
from langchain_community.embeddings import HuggingFaceInstructEmbeddings


hardCodeEmbeddings = HuggingFaceInstructEmbeddings(model_name="GanymedeNil/text2vec-large-chinese")


def z_score_normalization(scores):
    mean_score = np.mean(scores)
    std_deviation = np.std(scores)
    
    if std_deviation == 0 or np.isclose(std_deviation, 0):
        return scores
    else:
        normalized_scores = [(score - mean_score) / std_deviation for score in scores]
        return normalized_scores

def reciprocal_rank_fusion(results_a, results_b):
    k = 60  # RRF parameter; can be adjusted based on needs
    
    scores_a = z_score_normalization([doc['_score'] for doc in results_a])
    scores_b = z_score_normalization([doc['_score'] for doc in results_b])
    
    combined_results = results_a + results_b
    combined_scores = scores_a + scores_b
    
    rrf_scores = [1 / (k + score) for score in combined_scores]
    
    for i, doc in enumerate(combined_results):
        doc['rrf_score'] = rrf_scores[i]
    
    # 新增代码：过滤出每个 hash 值对应的最高分数的记录
    hash_to_best_doc = {}
    for doc in combined_results:
        doc_hash = doc['_source']['metadata']['hash']
        if doc_hash not in hash_to_best_doc or hash_to_best_doc[doc_hash]['rrf_score'] > doc['rrf_score']:
            hash_to_best_doc[doc_hash] = doc
    
    # 从 hash_to_best_doc 字典中提取最终的文档列表
    filtered_results = list(hash_to_best_doc.values())
    
    # 根据 RRF 分数排序，确保升序排序，即分数较低的记录排在前面
    filtered_results.sort(key=lambda x: x['rrf_score'])
    
    return filtered_results

def rrf(match_res, knn_res):
    match_hits = match_res['hits']['hits']
    knn_hits = knn_res['hits']['hits']
    
    merged_results = reciprocal_rank_fusion(match_hits, knn_hits)
    
    final_response = {
        'hits': {
            'total': {'value': len(merged_results), 'relation': 'eq'},
            'hits': merged_results
        }
    }
    return final_response

def search(index, kb_ids, query, size = 10, from_ = 0, hightlight = False):
    idxnm = f"ragflow_{index}"
    if not ELASTICSEARCH.indexExist(idxnm=idxnm):
        return f"Index with name: {idxnm} does not exist"
        
    match_res = ELASTICSEARCH.search(
                idxnm=f"{idxnm}",
                q={
                    "query": {
                        "bool": {
                            "must": [
                                {"match": {"text": query}}
                            ],
                            "filter": [
                                {"terms": {"kb_id": kb_ids}}
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
    
    knn_res = ELASTICSEARCH.search(
                idxnm=idxnm,
                q={
                    "query": {
                    "bool": {
                        "must": {
                            "knn": {
                                "field": "q_768_vec",
                                "query_vector": hardCodeEmbeddings.embed_query(query), 
                                "k": 10,
                                "num_candidates": 50,
                                "boost": 0.1
                            }
                        },
                        "filter": [
                            {"terms": {"kb_id": kb_ids}}
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
            
        ref = search(dia.tenant_id, dia.kb_ids, query)

        return get_json_result(data=ref)
    except Exception as e:
        return server_error_response(e)
