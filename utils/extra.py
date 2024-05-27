def convert_class_to_dict(data, showtrash=False):
    if showtrash ==False:
        data = data.__dict__.copy()
    new_data = {"contents": {}}
    for key in data["contents"]:
        if data["contents"][key].trash == showtrash:
            if data["contents"][key].type == "folder":
                new_data["contents"][key] = {
                    "name": data["contents"][key].name,
                    "type": data["contents"][key].type,
                    "id": data["contents"][key].id,
                    'path': data["contents"][key].path
                }
            else:
                new_data["contents"][key] = {
                    "name": data["contents"][key].name,
                    "type": data["contents"][key].type,
                    "size": data["contents"][key].size,
                    "id": data["contents"][key].id,
                    'path': data["contents"][key].path
                }

    return new_data


from datetime import datetime, timezone

def get_current_utc_time():
    return datetime.now(timezone.utc).strftime("Date - %Y-%m-%d | Time - %H:%M:%S")