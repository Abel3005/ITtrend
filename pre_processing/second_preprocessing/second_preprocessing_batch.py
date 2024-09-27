import json, boto3, logging, time
from botocore.exceptions import ClientError
import google.generativeai as genai
from farmhash import FarmHash32 as fhash
import asyncio
import time
import botocore.exceptions
from boto3.dynamodb.conditions import Key, Attr
from datetime import datetime
from logging_utils.logging_to_cloudwatch import log
import sys
from jobkorea import jobkorea
import pandas as pd
from tqdm import tqdm
from decimal import Decimal



logger = log('/aws/preprocessing/second/batch','logs')
with open("./.KEYS/SECOND_PREPROCESSING_KEY.json", "r") as f:
    key = json.load(f)
# S3 버킷 정보 get
with open("./.KEYS/DATA_SRC_INFO.json", "r") as f:
    bucket_info = json.load(f)
# GEMINI_API_KEY.tmp_key_list
with open("./.KEYS/tmp_key_list.json", "r") as f:
    gemini_api_key = json.load(f)
with open("./.DATA/PROMPT_INFO.json") as f:
    prompt_metadata = json.load(f)
    
async def send_data_async(chat_session,source_data,_response):
    prompt_data = prompt_metadata.get("data", {})
    task = []
    try:
        for idx, _obj in enumerate(source_data):
            _symbol = _obj.get('site_symbol', "").upper()
            if _symbol in prompt_data.keys():
                _data_source_keys = return_object_source_keys(prompt_data, _symbol)
                _prompt = return_object_prompt(prompt_data, _symbol).format(
                    data_source_keys=_data_source_keys, 
                    input_data=str(_obj)
                )
            task.append(asyncio.create_task(chat_session.send_message_async(_prompt)))
        await asyncio.sleep(60)
        for idx in range(len(source_data)):
            _response[idx] = await task[idx]
        try:
            for idx, _obj in enumerate(source_data):
                json_data = _response[idx].text.replace("```json\n", "").replace("\n```", "")
                dict_data = json.loads(json_data)
                data_item = return_concat_data_record(obj=_obj, data_dict=dict_data)
                upload_data(data_item)
        except Exception as e:
            logger.error(str(e))
    except Exception as e:
        logger.error(str(e))
        



# 지수 백오프를 포함한 스캔 작업
def scan_with_backoff(table, scan_kwargs):
    retry_attempts = 0
    max_retries = 10
    backoff_factor = 0.5
    source_data = []

    while True:
        try:
            response = table.scan(**scan_kwargs)
            source_data.extend(response.get('Items', []))
            last_evaluated_key = response.get('LastEvaluatedKey', None)
            if last_evaluated_key:
                scan_kwargs['ExclusiveStartKey'] = last_evaluated_key
            else:
                break
        except ClientError as error:
            if error.response['Error']['Code'] == 'ProvisionedThroughputExceededException':
                if retry_attempts < max_retries:
                    retry_attempts += 1
                    time.sleep(backoff_factor * (2 ** retry_attempts))  # 지수 백오프
                else:
                    raise
        except Exception as e:
            logger.error(f"An unexpected error occurred: {e}")
            break

    return source_data

def return_object_prompt(data, symbol_key):
    return data.get(symbol_key, {}).get("prompt")

def return_object_source_keys(data, symbol_key):
    return data.get(symbol_key, {}).get("source_key")

def return_concat_data_record(obj, data_dict):
    data_record = {
        "pid": obj.get("id"),
        "get_date": obj.get("get_date"),
        "site_symbol": obj.get("site_symbol"),
        "job_title": obj.get("job_title", None),
        "dev_stack": data_dict.get("dev_stack", []),
        "job_requirements": data_dict.get("job_requirements", []),
        "job_prefer": data_dict.get("job_prefer", []),
        "job_category": data_dict.get("job_category", []),
        "indurstry_type": data_dict.get("indurstry_type", []),
        "required_career": obj.get("required_career", None),
        "resume_required": obj.get("resume_required", None),
        "post_status": obj.get("post_status", None),
        "company_name": obj.get("company_name", None),
        "cid": fhash(obj.get("site_symbol")+obj.get("company_name")),
        "start_date": obj.get("start_date", None),
        "end_date": obj.get("end_date", None),
        "crawl_domain": obj.get("crawl_domain", None),
        "crawl_url": obj.get("crawl_url", None)
    }
    return data_record

def upload_data(_item):
    # DynamoDB 클라이언트 생성
    dynamodb = boto3.resource(
        'dynamodb',
        aws_access_key_id=key['aws_access_key_id'],
        aws_secret_access_key=key['aws_secret_key'],
        region_name=key['region']
    )
    table = dynamodb.Table("precessed-data-table")
    table.put_item(Item=_item)

    
async def main():
    dynamo_table_name = bucket_info['restore_table_name']
    dynamodb = boto3.resource(
            'dynamodb',
            aws_access_key_id=key['aws_access_key_id'],
            aws_secret_access_key=key['aws_secret_key'],
            region_name=key['region']
        )
    table = dynamodb.Table(dynamo_table_name)
    all_data = []
    response= table.scan(Select='ALL_ATTRIBUTES')
    all_data.extend(response.get("Items",[]))
    last_evaluated_key = response.get('LastEvaluatedKey', None)
    while last_evaluated_key:
        all_data.extend(response.get("Items",[]))
        response= table.scan(Select='ALL_ATTRIBUTES',ExclusiveStartKey=last_evaluated_key)
        last_evaluated_key = response.get('LastEvaluatedKey', None)
        time.sleep(5)
    _all = pd.DataFrame(all_data)
    _all= _all[_all['site_symbol'] != "JK" ]
    _all = _all.drop_duplicates(subset='id',keep='first')
    _all['indurstry_type'] = ""
    _all['job_category']= _all['job_category'].apply(lambda x: "" if type(x)== float else x)
    _all['required_career'] = _all['required_career'].apply(lambda x: False if type(x) != bool else x)
    _all['resume_required'] = _all['resume_required'].apply(lambda x: True if type(x) != bool else x)
    _all['start_date']=_all['start_date'].apply(lambda x: "" if type(x) != str else x)
    _all['post_status']= _all['post_status'].apply(lambda x: True if type(x) != bool else x)
    _all['status'] = _all['status'] == "released"
    _all['stacks'] = _all['stacks'].apply(lambda x: "" if type(x) != str else x)
    _all['job_prefer'] = _all['job_prefer'].apply(lambda x: "" if type(x) != str else x)
    _all['company_id'] = _all['company_id'].apply(lambda x: Decimal(x) if type(x) == float else x)
    table = dynamodb.Table('precessed-data-table')
    _data = []
    response= table.scan(Select='ALL_ATTRIBUTES')
    _data.extend(response.get("Items",[]))
    last_evaluated_key = response.get('LastEvaluatedKey', None)
    while last_evaluated_key:
        _data.extend(response.get("Items",[]))
        response= table.scan(Select='ALL_ATTRIBUTES',ExclusiveStartKey=last_evaluated_key)
        last_evaluated_key = response.get('LastEvaluatedKey', None)
        time.sleep(5)        
    precesed_data= set(map(lambda x: int(x['pid']), _data))
    precesed_data = set(map(Decimal,precesed_data))
    _filter = _all[_all['id'].apply(lambda x : int(x) not in precesed_data)]    
    unique_df = _filter.drop_duplicates(subset='id', keep='first').to_dict('records')
    batch_count = (len(unique_df) // 500) +1
    start_idx = 0
    for b in range(5):
        final_idx = min(len(unique_df),start_idx+500)
        source_data = unique_df[start_idx:final_idx]
        start_idx += 500
        key_list = gemini_api_key['GEMINI_API']
        genai.configure(api_key=key_list[b])
        # Create the model
        generation_config = {
            "temperature": 0.7,
            "top_p": 0.95,
            "top_k": 64,
            "max_output_tokens": 8192,
            "response_mime_type": "text/plain",
        }

        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash",
            generation_config=generation_config,
            # safety_settings = Adjust safety settings
            # # See https://ai.google.dev/gemini-api/docs/safety-settings
        )

        chat_session = model.start_chat(
            history=[

            ]
        )
        count = 10
        try:
            _response = [None for _ in range(count)]
            for i in range(len(source_data) // count):
                # 비동기 코드 실행
                print(i)
                await send_data_async(chat_session,source_data[(i*count):(i+1)*count],_response)
            if len(source_data)%count != 0:
                await send_data_async(chat_session,source_data[-(len(source_data)%count):],_response)
        except Exception as e:
            logger.error(f'{_response} : {e}')

if __name__ == '__main__':
    asyncio.run(main())
