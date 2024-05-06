#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import datetime
import json
import logging
import os
import hashlib
import copy
import re
import sys
import time
import traceback
from functools import partial
from rag.utils import MINIO
from api.db.db_models import close_connection
from rag.settings import database_logger
from rag.settings import cron_logger, DOC_MAXIMUM_SIZE
from multiprocessing import Pool
import numpy as np
from elasticsearch_dsl import Q
from multiprocessing.context import TimeoutError
from api.db.services.task_service import TaskService
from rag.utils import ELASTICSEARCH
from timeit import default_timer as timer
from rag.utils import rmSpace, findMaxTm

from rag.nlp import search
from io import BytesIO
import pandas as pd

from rag.app import laws, paper, presentation, manual, qa, table, book, resume, picture, naive, one

from api.db import LLMType, ParserType
from api.db.services.document_service import DocumentService
from api.db.services.llm_service import LLMBundle
from api.utils.file_utils import get_project_base_directory
from rag.utils.redis_conn import REDIS_CONN

BATCH_SIZE = 64

FACTORY = {
    "general": naive,
    ParserType.NAIVE.value: naive,
    ParserType.PAPER.value: paper,
    ParserType.BOOK.value: book,
    ParserType.PRESENTATION.value: presentation,
    ParserType.MANUAL.value: manual,
    ParserType.LAWS.value: laws,
    ParserType.QA.value: qa,
    ParserType.TABLE.value: table,
    ParserType.RESUME.value: resume,
    ParserType.PICTURE.value: picture,
    ParserType.ONE.value: one,
}


def set_progress(task_id, from_page=0, to_page=-1,
                 prog=None, msg="Processing..."):
    if prog is not None and prog < 0:
        msg = "[ERROR]" + msg
    cancel = TaskService.do_cancel(task_id)
    if cancel:
        msg += " [Canceled]"
        prog = -1

    if to_page > 0:
        if msg:
            msg = f"Page({from_page+1}~{to_page+1}): " + msg
    d = {"progress_msg": msg}
    if prog is not None:
        d["progress"] = prog
    try:
        TaskService.update_progress(task_id, d)
    except Exception as e:
        cron_logger.error("set_progress:({}), {}".format(task_id, str(e)))

    if cancel:
        sys.exit()


def collect(comm, mod, tm):
    tasks = TaskService.get_tasks(tm, mod, comm)
    #print(tasks)
    if len(tasks) == 0:
        time.sleep(1)
        return pd.DataFrame()
    tasks = pd.DataFrame(tasks)
    mtm = tasks["update_time"].max()
    cron_logger.info("TOTAL:{}, To:{}".format(len(tasks), mtm))
    return tasks


def get_minio_binary(bucket, name):
    global MINIO
    if REDIS_CONN.is_alive():
        try:
            r = REDIS_CONN.get("{}/{}".format(bucket, name))
            if r: return r
        except Exception as e:
            cron_logger.warning("Get redis[EXCEPTION]:" + str(e))
    return MINIO.get(bucket, name)


def build(row):
    if row["size"] > DOC_MAXIMUM_SIZE:
        set_progress(row["id"], prog=-1, msg="File size exceeds( <= %dMb )" %
                     (int(DOC_MAXIMUM_SIZE / 1024 / 1024)))
        return []

    callback = partial(
        set_progress,
        row["id"],
        row["from_page"],
        row["to_page"])
    chunker = FACTORY[row["parser_id"].lower()]
    pool = Pool(processes=1)
    try:
        st = timer()
        thr = pool.apply_async(get_minio_binary, args=(row["kb_id"], row["location"]))
        binary = thr.get(timeout=90)
        pool.terminate()
        cron_logger.info(
            "From minio({}) {}/{}".format(timer()-st, row["location"], row["name"]))
        cks = chunker.chunk(row["name"], binary=binary, from_page=row["from_page"],
                            to_page=row["to_page"], lang=row["language"], callback=callback,
                            kb_id=row["kb_id"], parser_config=row["parser_config"], tenant_id=row["tenant_id"])
        cron_logger.info(
            "Chunkking({}) {}/{}".format(timer()-st, row["location"], row["name"]))
    except TimeoutError as e:
        callback(-1, f"Internal server error: Fetch file timeout. Could you try it again.")
        cron_logger.error(
            "Chunkking {}/{}: Fetch file timeout.".format(row["location"], row["name"]))
        return
    except Exception as e:
        if re.search("(No such file|not found)", str(e)):
            callback(-1, "Can not find file <%s>" % row["name"])
        else:
            callback(-1, f"Internal server error: %s" %
                     str(e).replace("'", ""))
        pool.terminate()
        traceback.print_exc()

        cron_logger.error(
            "Chunkking {}/{}: {}".format(row["location"], row["name"], str(e)))

        return

    docs = []
    doc = {
        "doc_id": row["doc_id"],
        "kb_id": [str(row["kb_id"])]
    }
    el = 0
    for ck in cks:
        d = copy.deepcopy(doc)
        d.update(ck)
        md5 = hashlib.md5()
        md5.update((ck["content_with_weight"] +
                   str(d["doc_id"])).encode("utf-8"))
        d["_id"] = md5.hexdigest()
        d["create_time"] = str(datetime.datetime.now()).replace("T", " ")[:19]
        d["create_timestamp_flt"] = datetime.datetime.now().timestamp()
        if not d.get("image"):
            docs.append(d)
            continue

        output_buffer = BytesIO()
        if isinstance(d["image"], bytes):
            output_buffer = BytesIO(d["image"])
        else:
            d["image"].save(output_buffer, format='JPEG')

        st = timer()
        MINIO.put(row["kb_id"], d["_id"], output_buffer.getvalue())
        el += timer() - st
        d["img_id"] = "{}-{}".format(row["kb_id"], d["_id"])
        del d["image"]
        docs.append(d)
    cron_logger.info("MINIO PUT({}):{}".format(row["name"], el))

    return docs


def init_kb(row):
    idxnm = search.index_name(row["tenant_id"])
    if ELASTICSEARCH.indexExist(idxnm):
        return
    return ELASTICSEARCH.createIdx(idxnm, json.load(
        open(os.path.join(get_project_base_directory(), "conf", "mapping.json"), "r")))


def embedding(docs, mdl, parser_config={}, callback=None):
    batch_size = 32
    tts, cnts = [rmSpace(d["title_tks"]) for d in docs if d.get("title_tks")], [
        re.sub(r"</?(table|td|caption|tr|th)( [^<>]{0,12})?>", " ", d["content_with_weight"]) for d in docs]
    tk_count = 0
    if len(tts) == len(cnts):
        tts_ = np.array([])
        for i in range(0, len(tts), batch_size):
            vts, c = mdl.encode(tts[i: i + batch_size])
            if len(tts_) == 0:
                tts_ = vts
            else:
                tts_ = np.concatenate((tts_, vts), axis=0)
            tk_count += c
            callback(prog=0.6 + 0.1 * (i + 1) / len(tts), msg="")
        tts = tts_

    cnts_ = np.array([])
    for i in range(0, len(cnts), batch_size):
        vts, c = mdl.encode(cnts[i: i + batch_size])
        if len(cnts_) == 0:
            cnts_ = vts
        else:
            cnts_ = np.concatenate((cnts_, vts), axis=0)
        tk_count += c
        callback(prog=0.7 + 0.2 * (i + 1) / len(cnts), msg="")
    cnts = cnts_

    title_w = float(parser_config.get("filename_embd_weight", 0.1))
    vects = (title_w * tts + (1 - title_w) *
             cnts) if len(tts) == len(cnts) else cnts

    assert len(vects) == len(docs)
    for i, d in enumerate(docs):
        v = vects[i].tolist()
        d["q_%d_vec" % len(v)] = v
    return tk_count


def main(comm, mod):
    tm_fnm = os.path.join(
        get_project_base_directory(),
        "rag/res",
        f"{comm}-{mod}.tm")
    tm = findMaxTm(tm_fnm)
    rows = collect(comm, mod, tm)
    if len(rows) == 0:
        return

    tmf = open(tm_fnm, "a+")
    for _, r in rows.iterrows():
        callback = partial(set_progress, r["id"], r["from_page"], r["to_page"])
        #callback(random.random()/10., "Task has been received.")
        try:
            embd_mdl = LLMBundle(r["tenant_id"], LLMType.EMBEDDING, llm_name=r["embd_id"], lang=r["language"])
        except Exception as e:
            traceback.print_stack(e)
            callback(prog=-1, msg=str(e))
            continue

        st = timer()
        cks = build(r)
        cron_logger.info("Build chunks({}): {}".format(r["name"], timer()-st))
        if cks is None:
            continue
        if not cks:
            tmf.write(str(r["update_time"]) + "\n")
            callback(1., "No chunk! Done!")
            continue
        # TODO: exception handler
        ## set_progress(r["did"], -1, "ERROR: ")
        callback(
            msg="Finished slicing files(%d). Start to embedding the content." %
            len(cks))
        st = timer()
        try:
            tk_count = embedding(cks, embd_mdl, r["parser_config"], callback)
        except Exception as e:
            callback(-1, "Embedding error:{}".format(str(e)))
            print(str(e))
            cron_logger.error(str(e))
            tk_count = 0
        cron_logger.info("Embedding elapsed({}): {}".format(r["name"], timer()-st))

        callback(msg="Finished embedding({})! Start to build index!".format(timer()-st))
        init_kb(r)
        chunk_count = len(set([c["_id"] for c in cks]))
        st = timer()
        es_r = ELASTICSEARCH.bulk(cks, search.index_name(r["tenant_id"]))
        cron_logger.info("Indexing elapsed({}): {}".format(r["name"], timer()-st))
        if es_r:
            callback(-1, "Index failure!")
            ELASTICSEARCH.deleteByQuery(
                Q("match", doc_id=r["doc_id"]), idxnm=search.index_name(r["tenant_id"]))
            cron_logger.error(str(es_r))
        else:
            if TaskService.do_cancel(r["id"]):
                ELASTICSEARCH.deleteByQuery(
                    Q("match", doc_id=r["doc_id"]), idxnm=search.index_name(r["tenant_id"]))
                continue
            callback(1., "Done!")
            DocumentService.increment_chunk_num(
                r["doc_id"], r["kb_id"], tk_count, chunk_count, 0)
            cron_logger.info(
                "Chunk doc({}), token({}), chunks({}), elapsed:{}".format(
                    r["id"], tk_count, len(cks), timer()-st))

        tmf.write(str(r["update_time"]) + "\n")
    tmf.close()


if __name__ == "__main__":
    peewee_logger = logging.getLogger('peewee')
    peewee_logger.propagate = False
    peewee_logger.addHandler(database_logger.handlers[0])
    peewee_logger.setLevel(database_logger.level)

    #from mpi4py import MPI
    #comm = MPI.COMM_WORLD
    while True:
        main(int(sys.argv[2]), int(sys.argv[1]))
        close_connection()
