"""在 Ubuntu 服务器本地交互配置，不输出密钥，不访问网络。"""
import getpass
import os
from pathlib import Path


def read_env(path):
    values={}
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                name,value=line.split("=",1)
                values[name.strip()]=value.strip().strip("\"'")
    return values


def main():
    os.umask(0o077)
    target=Path(__file__).resolve().parent/".env"
    values=read_env(target)
    old=read_env(Path("/home/ubuntu/wecom-deepseek/.env"))
    for key in ("DEEPSEEK_API_KEY","DEEPSEEK_MODEL","DEEPSEEK_BASE_URL"):
        if key not in values and old.get(key):
            values[key]=old[key]
    print("所有输入均隐藏；粘贴后看不到字符属于正常情况，按回车确认。")
    print("已有配置可直接按回车保留。请勿发送填写凭证的截图。")
    for name,label in [
        ("WECOM_CORP_ID","企业ID（我的企业→企业信息中，通常以 ww 开头）"),
        ("CALLBACK_TOKEN","回调 Token（企微 API 接收页面随机生成）"),
        ("CALLBACK_AES_KEY","EncodingAESKey（同一页面随机生成，43位）"),
        ("WECOM_APP_SECRET","新自建应用“小小艺 AI 客服”的 Secret"),
    ]:
        while True:
            new=getpass.getpass(label+"：").strip()
            value=new or values.get(name,"")
            if value and value.isascii() and not any(c.isspace() for c in value) and "=" not in value:
                if name!="CALLBACK_AES_KEY" or len(value)==43:
                    values[name]=value
                    break
            print("格式不正确，请重新输入（不会显示具体值）。")
    if not values.get("DEEPSEEK_API_KEY"):
        values["DEEPSEEK_API_KEY"]=getpass.getpass("DeepSeek API Key：").strip()
    values.setdefault("DEEPSEEK_MODEL","deepseek-flash")
    values.setdefault("DEEPSEEK_BASE_URL","https://api.deepseek.com")
    values.setdefault("ENABLE_AI","false")
    values.setdefault("KF_ACCOUNT_IDS","")
    target.write_text("".join(key+"="+value+"\n" for key,value in values.items()),encoding="utf-8")
    target.chmod(0o600)
    print("配置已保存。AI 回复："+values["ENABLE_AI"]+"。可以启动 URL 验证服务。")


if __name__=="__main__": main()
