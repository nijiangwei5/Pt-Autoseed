# ！/usr/bin/python3
# -*- coding: utf-8 -*-

import json
import re
import time
import os
from http.cookies import SimpleCookie

import pymysql
import transmissionrpc
import requests
from bs4 import BeautifulSoup


# Config
class JSONObject:
    def __init__(self, d):
        self.__dict__ = d


setting = json.loads(open("settings.json", "r").read(), object_hook=JSONObject)
tc = transmissionrpc.Client(address=setting.trans_address, port=setting.trans_port, user=setting.trans_user,
                            password=setting.trans_password)
db = pymysql.connect(host=setting.db_address, port=setting.db_port, user=setting.db_user, password=setting.db_password,
                     db=setting.db_name, charset='utf8')
cookie = SimpleCookie(setting.byr_cookies)
cookies = {}
for key, morsel in cookie.items():
    cookies[key] = morsel.value

search_pattern = re.compile("(?P<full_name>(?P<search_name>.+?)\.(?P<tv_season>[S|s]\d+(?:(?:[E|e]\d+)|(?:[E|e]\d+-[E|e]\d+)))\..+?-(?P<group>.+?))\.(?P<tv_filetype>mkv)")


# 提交SQL语句
def commit_cursor_into_db(sql):
    cursor = db.cursor()
    try:
        cursor.execute(sql)
        db.commit()
        cursor.close()
    except:
        db.rollback()


# 找到最后的发布种子
def find_max(column, table):
    cursor = db.cursor()
    sql = "SELECT MAX(" + column + ") FROM `" + table + "`"
    cursor.execute(sql)
    result = cursor.fetchall()
    cursor.close()
    t = result[0][0]
    if not t:
        t = 0
    return t


# 游标获取全部seed_list
def get_table_seed_list():
    cursor = db.cursor()
    sql = "SELECT id,download_id,seed_id FROM seed_list"
    cursor.execute(sql)
    return_info = cursor.fetchall()
    cursor.close()
    return return_info


def update_torrent_info_from_rpc_to_db():
    cursor = db.cursor()
    sql = "SELECT id,title FROM seed_list"
    cursor.execute(sql)
    result = cursor.fetchall()
    cursor.close()
    title_list = []
    for i in result:
        title_list.append(i[1])
    last_seed_id = find_max("seed_id", "seed_list")
    for t in tc.get_torrents():
        if t.id > last_seed_id:
            if t.name in title_list:
                sort_id = result[title_list.index(t.name)][0]
                if t.trackers[0]["announce"].find("tracker.byr.cn") != -1:
                    sql = "UPDATE seed_list SET seed_id = '%d' WHERE id = '%d'" % (t.id, sort_id)
                    commit_cursor_into_db(sql)
            elif t.trackers[0]["announce"].find("tracker.byr.cn") == -1:
                sql = "INSERT INTO seed_list (title,download_id) VALUES ('%s','%d')" % (t.name, t.id)
                commit_cursor_into_db(sql)
    print("Update torrent info from rpc to db OK~")


# 从transmission和数据库中删除种子及其数据
def del_torrent_with_data_and_db():
    result = get_table_seed_list()
    for t in result:
        if t[2] != 0:
            seed_torrent = tc.get_torrent(t[2])
            if seed_torrent.rateUpload != 0:  # seed_torrent没有上传速度 -> 种子达到最小发布时间 -> 种子达到最大发布时间或达到设定分享率 -> 删除种子
                if (int(time.time()) - seed_torrent.doneDate) >= setting.torrent_minSeedTime and (seed_torrent.uploadRatio >= setting.torrent_maxUploadRatio or (int(time.time()) - seed_torrent.doneDate) >= setting.torrent_maxSeedTime):
                    sql = "DELETE FROM seed_list WHERE id = '%d'" % (t[0])
                    commit_cursor_into_db(sql)
                    tc.remove_torrent(t[2], delete_data=True)
                    tc.remove_torrent(t[1])
                    print("Delete torrent: '%d' '%d',Which name '%s'") % (t[1], t[2], seed_torrent.name)


# 从数据库中获取剧集简介
def get_info_from_db(torrent_search_name):
    cursor = db.cursor()
    sql = "SELECT * FROM tv_info WHERE tv_ename='%s'" % torrent_search_name
    cursor.execute(sql)
    result = list(cursor.fetchall()[0])
    cursor.close()
    return result


# 如果种子在byr存在，返回种子id，不存在返回0
def exist_judge(tid):
    tag = 0
    t = tc.get_torrent(tid)
    torrent_name = t.name
    torrent_info_search = re.search(search_pattern, torrent_name)
    if torrent_info_search:
        full_name = torrent_info_search.group("full_name")
        exits_judge_raw = requests.get(
            url="http://bt.byr.cn/torrents.php?secocat=&cat=&incldead=0&spstate=0&inclbookmarked=0&search=" + full_name + "&search_area=0&search_mode=0",
            cookies=cookies)
        bs = BeautifulSoup(exits_judge_raw.text, "html5lib")
        if bs.find_all(text=re.compile("没有种子")):  # 如果不存在种子
            tag = 0  # 强调一遍。。。。
        elif bs.find_all("a", href=re.compile("download.php"))[0]["href"]:  # 如果存在（还有人比Autoseed快。。。
            href = bs.find_all("a", href=re.compile("download.php"))[0]["href"]
            torrent_download_id = re.search("id=(\d+)", href).group(1)  # 找出种子id
            tid_info = BeautifulSoup(  # 从该种子的detail.php页面，获取主标题
                requests.get("http://bt.byr.cn/details.php?id=" + torrent_download_id, cookies=cookies).text,
                "html5lib")
            tid_info_title_text = tid_info.title.get_text()  # get主标题
            if re.search(re.compile(full_name), tid_info_title_text):  # 确认主标题（二步确认？）
                tag = torrent_download_id
        return tag


def get_torrent_from_reseed_tracker_and_add_it_to_transmission_with_db_update(torrent_download_id):
    download_torrent_link = "http://bt.byr.cn/download.php?id=" + torrent_download_id
    torrent_file = requests.get(download_torrent_link, cookies=cookies)  # 下载种子
    with open(setting.trans_watchdir + "/" + torrent_download_id + ".torrent.get", "wb") as code:
        code.write(torrent_file.content)  # 保存种子文件到watch目录
    os.rename(setting.trans_watchdir + "/" + torrent_download_id + ".torrent.get",
              setting.trans_watchdir + "/" + torrent_download_id + ".torrent")  # 下载完成后，重命名成正确的后缀名
    print("Download Torrent which id = " + torrent_download_id + "OK!")
    time.sleep(5)  # 等待transmission读取种子文件
    new_torrent_id = tc.get_torrents()[-1].id  # 获取种子id
    while True:
        if tc.get_torrent(new_torrent_id).status == 'seeding':  # 当种子状态为“seeding”（重发布完成）
            requests.post(url="http://bt.byr.cn/thanks.php", cookies=cookies,
                          data={"id": str(torrent_download_id)})  # 自动感谢发布者。。。。。
            update_torrent_info_from_rpc_to_db()  # 更新数据库
            break


# 发布
def seed_post(tid):
    tag = exist_judge(tid)
    if tag == 0:  # 种子不存在，则准备发布
        t = tc.get_torrent(tid)
        torrent_full_name = t.name
        torrent_name = re.search("torrents/(.+?\.torrent)", t.torrentFile).group(1)
        torrent_info_search = re.search(search_pattern, torrent_full_name)
        try:
            torrent_info_raw_from_db = get_info_from_db(torrent_info_search.group("search_name"))  # 从数据库中获取该美剧信息
        except IndexError:  # 数据库没有该种子数据
            print("Not Find info of torrent: " + torrent_name + ",Stop post!!")
            sql = "UPDATE seed_list SET seed_id = -1 WHERE download_id='%d'" % t[0]
            commit_cursor_into_db(sql)
        else:  # 数据库中有该剧集信息
            multipart_data = (
                ("type", ('', str(torrent_info_raw_from_db[1]))),
                ("second_type", ('', str(torrent_info_raw_from_db[2]))),
                ("file", (torrent_name, open(t.torrentFile, 'rb'), 'application/x-bittorrent')),
                ("tv_type", ('', str(torrent_info_raw_from_db[4]))),
                ("cname", ('', torrent_info_raw_from_db[5])),
                ("tv_ename", ('', torrent_info_search.group("full_name"))),
                ("tv_season", ('', torrent_info_search.group("tv_season"))),
                ("tv_filetype", ('', torrent_info_raw_from_db[8])),
                ("type", ('', str(torrent_info_raw_from_db[9]))),
                ("small_descr", ('', torrent_info_raw_from_db[10])),
                ("url", ('', torrent_info_raw_from_db[11])),
                ("dburl", ('', torrent_info_raw_from_db[12])),
                ("nfo", ('', torrent_info_raw_from_db[13])),
                ("descr", ('', torrent_info_raw_from_db[14])),
                ("uplver", ('', torrent_info_raw_from_db[15])),
            )
            while True:  # 检查下载种子情况,等待种子下载完成
                if tc.get_torrent(tid).status == "seeding":
                    break
            # 发布种子
            post = requests.post(url="http://bt.byr.cn/takeupload.php", cookies=cookies, files=multipart_data)
            if post.url != "http://bt.byr.cn/takeupload.php":  # 发布检查
                seed_torrent_download_id = re.search("id=(\d+)", post.url).group(1)  # 获取种子编号
                print("Post OK,the torrent id :" + seed_torrent_download_id)
                get_torrent_from_reseed_tracker_and_add_it_to_transmission_with_db_update(
                    seed_torrent_download_id)  # 下载种子，并更新
    else:  # 如果种子存在（已经有人发布）  -> 辅种
        get_torrent_from_reseed_tracker_and_add_it_to_transmission_with_db_update(tag)


def seed_judge():
    result = get_table_seed_list()  # 从数据库中获取seed_list(tuple:(id,download_id,seed_id))
    for t in result:  # 遍历seed_list
        if t[2] == 0:  # 如果种子没有被重发布过(t[2] == 0)    ,另不发布(t[2] == -1)
            torrent = tc.get_torrent(t[1])  # 获取下载种子信息
            torrent_full_name = torrent.name
            print("New get torrent:" + torrent_full_name)
            torrent_info_search = re.search(search_pattern, torrent_full_name)
            if torrent_info_search:  # 如果种子名称结构符合search_pattern（即属于剧集）
                print("Begin post~")
                seed_post(t[1])  # 发布种子
                update_torrent_info_from_rpc_to_db()
            else:  # 不符合，更新seed_id为-1
                sql = "UPDATE seed_list SET seed_id = -1 WHERE id='%d'" % t[0]
                commit_cursor_into_db(sql)


def main():
    print("Autoseed start~")
    i = 0
    while True:
        print("Check time " + str(i) + " At Time: " + str(time.asctime(time.localtime(time.time()))))
        update_torrent_info_from_rpc_to_db()
        seed_judge()
        del_torrent_with_data_and_db()
        time.sleep(setting.sleep_time)
        i += 1


if __name__ == '__main__':
    main()