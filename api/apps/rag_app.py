from flask import request
from flask_login import login_required
from api.db.services.dialog_service import DialogService, use_retrival
from api.utils.api_utils import server_error_response, get_data_error_result, validate_request
from api.utils.api_utils import get_json_result

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

    msg = []
    for m in req["messages"]:
        if m["role"] == "system":
            system = m["content"]
            continue
        if m["role"] == "assistant" and not msg:
            continue
        msg.append({"role": m["role"], "content": m["content"]})
        query = m["content"]

    try:
        e, dia = DialogService.get_by_name(agent)
        if not e:
            return get_data_error_result(retmsg="Dialog not found!")
        del req["messages"]
        ref = use_retrival(dia, msg, **req)

        return get_json_result(data=ref)
    except Exception as e:
        return server_error_response(e)