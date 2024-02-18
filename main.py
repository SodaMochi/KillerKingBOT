import json
import discord
import re
from discord.interactions import Interaction
from discord.ui import Select,View,Button
#from discord_components import Button, Select, SelectOption, ComponentsBot
import random
import datetime
import os
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ['TOKEN']
client = discord.Client(intents=discord.Intents.all())
#bot = ComponentsBot("!")

with open('role.json') as f:
    role_data = json.load(f)
with open('player.json') as f:
    player_data = json.load(f)
    
# 現在オンラインのGame(guild -> Game)
games = dict()
    
# 役職名リスト（ラベル用）
roles_name = [item for item in role_data]

class Role:
    #注意: 初期プレイヤー名はリストで渡す
    def __init__(self,role_name:str,player_names:str,ability_usage:int):
        self.role_name:str = role_name
        self.player_name:list = player_names
        self.remaining_ability_usage:int = ability_usage #能力使用回数
        #self.answer_status = list()
        self.is_ability_blocked:bool = False #エースの妨害を受けているか/能力入れ替えでリセット
        
    #ヘルプメッセージを(項目:本文)の辞書で返す
    #Spade2(能力の残り使用回数->無制限), Joker(脱出条件)はオーバーライドする
    def GetHelpMessage(self) -> dict:
        res = dict()
        res["役職"] = self.role_name
        res["能力"] = role_data[self.role_name]["ability_description"]
        res["能力の残り使用回数"] = self.remaining_ability_usage
        res["脱出条件"] = role_data[self.role_name]["escape_condition"]
        if self.is_ability_blocked: res["備考"] = "能力の使用が妨害されている"
        return res
    
class Player:
    def __init__(self,player_name:str,role_name:str):
        self.player_name:str = player_name
        self.role_name:str = role_name
        self.channel:discord.TextChannel = None
        self.sent_roles = list()
        self.replyable_roles = list()
        self.waiting_embed:discord.Message = None #入力待ち中にコマンドを入力されたときに処理を中断する用
        
    #ヘルプメッセージを(項目:本文)の辞書で返す
    def GetHelpMessage(self,role_list:list) -> dict: #role_list: ゲーム内のRolesリスト
        DMable_roles = list()
        for item in role_list:
            if not item.role_name in self.sent_roles: DMable_roles.append(item.role_name)
        res = dict()
        res["あなたの名前"] = self.player_name
        res["DMを送信可能"] = DMable_roles
        return res
    
    #現在実行中のView(Button,Select...)を中止し、エラーメッセージに差し替える
    def CancelView(self):
        #待機中のViewがなければそのまま
        if self.waiting_embed==None: return
        #if self.waiting_embed==None: raise Exception("No active process found.")
        
        self.waiting_embed.edit(embed=GetErrorEmbed("中断しました"))
        self.waiting_embed = None
        
    #「DM」を送るためのフォーム
    # Mikado(特定個人にしか送れない)、Doki(Jokerに無制限に遅れる)はオーバーライドする
    async def SendMessageInputForm(self,game):
        # 送信フォームを送信
        # embed: 入力内容を表示    view: 入力ボタン、送信先選択、送信ボタン
        view = MessageInputForm(game,self)
        await self.channel.send(embed=view.GenerateInputStatus(),view=view)
        
    async def SendReplyInputForm(self):
        return
    
    # メッセージを送信 成功したらTrue -> 呼び出し側がReceiveMessageを続けて呼ぶ
    # 入力フォームから呼ばれる
    async def SendMessage(self,address_role:str,content:str,is_reply:bool=False) -> bool:
        if is_reply:
            if not address_role in self.replyable_roles:
                await SendError(self.channel,'返信するには相手からメッセージを受け取っており、まだ返信していないことが必要です')
                return False
            await SendSystemMessage(self.channel,'以下のメッセージを送信しました',headline=f'{self.role_name}から返信が届きました',content=content)
            self.replyable_roles.remove(address_role)
        else:
            if address_role in self.sent_roles:
                await SendError(self.channel,'既にメッセージを送信済みの役職です')
                return False
            await SendSystemMessage(self.channel,'以下のメッセージを送信しました',headline=f'{self.role_name}からメッセージが届きました',content=content)
            self.sent_roles.append(address_role)
        return True
    
    # 受信側
    async def ReceiveMessage(self,sender_role:str,content:str,is_reply:bool=False):
        if is_reply:
            await SendCustumMessage(self.channel,title=f'{sender_role}から返信が届きました',content=content,color=0x7B68EE)
        else:
            await SendCustumMessage(self.channel,title=f'{sender_role}からメッセージが届きました',content=f'(!reply で返信できます)\n\n{content}',color=0x7B68EE)
            self.replyable_roles.append(sender_role)
        
# ゲーム内の「DM」とその「返信」のフォーマット
def GetMessageEmbed(sender:str,content:str):
    return discord.Embed(title=f'{sender}からメッセージが届きました',description=content,color=0x7B68EE)
def GetReplyEmbed(sender:str,content:str):
    return discord.Embed(title=f'{sender}から返信が届きました',description=f'(!reply で返信できます)\n\n{content}',color=0x7B68EE)

        
'''
    ユーザーとのやり取り
'''
# 引数のチャンネルからの入力を待ち、返す
# コマンドを打たれた場合はException: "CommandInputWhileWaiting"を返す
async def WaitForResponse(textchannel:discord.TextChannel):
    def check(msg:discord.Message): return textchannel==msg.channel and not msg.author.bot
    msg = await client.wait_for('message',check=check)
    if msg.content.startswith("!"): raise Exception("CommandInputWhileWaiting")
    return msg.content

# 指定のテキストチャンネルに「System Message」を送る（ゲーム上の演出）
# 後で編集可能にするため、返り値として送信メッセージのインスタンスを返す
async def SendSystemMessage(textchannel:discord.TextChannel,description='',headline='',content=''):
    embed =  discord.Embed(title='System Message',description=description,color=0x4169E1)
    if content or headline: embed.add_field(name=headline,value=content)
    return await textchannel.send(embed=embed)

# 指定のテキストチャンネルに「Error」を送る（ゲーム上の演出）
# 後で編集可能にするため、返り値として送信メッセージのインスタンスを返す
async def SendError(textchannel:discord.TextChannel,content:str):
    embed = discord.Embed(title='Error',description=content,color=0xFF0000)
    return await textchannel.send(embed=embed)
# embedのみ返す版(edit_message用)
def GetErrorEmbed(content:str):
    return discord.Embed(title='Error',description=content,color=0xFF0000)

# その他カスタムメッセージ
# 引数でチャンネル、タイトル、本文、色を指定
async def SendCustumMessage(textchannel:discord.TextChannel,title:str,content:str,color:int):
    embed = discord.Embed(title=title,description=content,color=color)
    return await textchannel.send(embed=embed)
'''
    ゲーム
'''
class Game:
    def __init__(self,loby:discord.TextChannel):
        # lobyは接続時に最初に発言したチャンネルに固定する(以前のチャンネル全てが削除されてやり取り不可になるのを避けるため)
        self.loby:discord.TextChannel = loby
        
        self.admin:discord.TextChannel = None
        # {name : value}
        self.Roles:dict = dict()
        self.Players:dict = dict()
        # "ゲーム開始前" -> "ゲーム進行中" -> "ゲーム終了"
        self.phase = "ゲーム開始前"
        self.time_in_game = 0
        
        for name,role in role_data.items():
            self.Roles[name] = Role(name,role["initial_player_name"],role["ability_usage_count"])
        for name,player in player_data.items():
            self.Players[name] = Player(name,player["initial_role"])
          
    # セーブデータをファイルに書き込む(BOT再起動時にデータを持ち越すため)  
    def Save(self):
        save = dict()
        # Gameのデータ
        save["loby"] = self.loby.id
        if self.admin:
            save["admin_id"] = self.admin.id
        else:
            save["admin_id"] = None
        save["phase"] = self.phase
        save["time_in_game"] = self.time_in_game    
        # 各Player, Roleのデータ
        roles = dict()
        for role in self.Roles.values():
            d = dict()
            d["player_name"] = role.player_name
            d["remaining_ability_usage"] = role.remaining_ability_usage
            d["is_ability_blocked"] = role.is_ability_blocked
            roles[role.role_name] = d
        save["roles"] = roles
        
        players = dict()
        for player in self.Players.values():
            d = dict()
            d["role_name"] = player.role_name
            if player.channel:
                d["channel_id"] = player.channel.id
            else:
                d["channel_id"] = None
            d["sent_roles"] = player.sent_roles
            d["replyable_roles"] = player.replyable_roles
            players[player.player_name] = d
        save["players"] = players
        
        # ファイル書き込み
        with open('save_data.json','r') as f:
            try:
                save_data = json.load(f)
            except:
                save_data = dict()
        with open('save_data.json','w') as f:
            save_data[str(self.loby.guild.id)] = save
            json.dump(save_data,f,indent=4)
    
    # セーブデータを読み込み、反映する
    def Load(self,guild_data:json):
        self.loby = client.get_channel(guild_data["loby"])
        if guild_data["admin_id"]:
            self.admin = client.get_channel(guild_data["admin_id"])
        self.phase = guild_data["phase"]
        self.time_in_game = guild_data["time_in_game"]
        
        for role in self.Roles.values():
            data = guild_data["roles"][role.role_name]
            role.player_name = data["player_name"]
            role.remaining_ability_usage = data["remaining_ability_usage"]
            role.is_ability_blocked = data["is_ability_blocked"]
        for player in self.Players.values():
            data = guild_data["players"][player.player_name]
            player.role_name = data["role_name"]
            if data["channel_id"]:
                player.channel = client.get_channel(data["channel_id"])
            player.sent_roles = data["sent_roles"]
            player.replyable_roles = data["replyable_roles"]
    
    async def Interpret(self,message:discord.Message):
        if not message.content.startswith("!"): return
        cmd = message.content[1:]
        
        if cmd=="set":
            await self.SetChannel(message.channel)
        elif cmd=="save":
            self.Save()
            await SendSystemMessage(message.channel,'進行状況を保存しました')
        
        author = ""
        if message.channel==self.loby: author = "loby"
        elif message.channel==self.admin: author = "admin"
        elif message.channel in map(lambda x:x.channel, self.Players.values()): author = "player"
        if not author: return
        print(author) #debug
        
        if author=="player":
            for person in self.Players.values():
                if message.channel==person.channel: player:Player = person
                
            # 本来はゲーム中コマンド
            
            # プレイヤー用コマンド
            if cmd=="dm" or cmd=="DM": await player.SendMessageInputForm(self)
            
    async def SetChannel(self,channel:discord.TextChannel):
        # 既に割当済み
        occupied = ''
        if channel==self.admin: occupied = 'admin'
        elif channel==self.loby: occupied = 'loby'
        for player in self.Players.values():
            if channel==player.channel: occupied = player.player_name
        if occupied:
            await SendError(channel,f'このチャンネルは既に{occupied}として登録されています。先に新しい{occupied}のチャンネルを登録してください')
            return
        
        await SendSystemMessage(channel,"チャンネル名を入力してください(ゲームマスター用は「admin」、その他はプレイヤー名を入力)")
        res = await WaitForResponse(channel)
        if res=="admin" or res=="Admin" or res=="ADMIN":
            self.admin = channel
            await SendSystemMessage(channel,"adminチャンネルを設定しました")
            return
        if res=="loby" or res=="Loby" or res=="LOBY":
            self.loby = channel
            await SendSystemMessage(channel,"lobyを設定しました")
            return
        for name,player in player_data.items():
            if res in player["name_variants"]: 
                self.Players[name].channel = channel
                await SendSystemMessage(channel,f"{name}のチャンネルを設定しました")
                return
        await SendError(channel,"該当のチャンネル名が見つかりません\n漢字、ひらがな、名字、名前、フルネームのいずれかで入力してください")
        
'''
    discord.ui
'''
# Player.SendDMで用いる
# ReplyInputFormを継承してつくる？
class MessageInputForm(View):
    def __init__(self,game:Game,sender:Player):
        super().__init__(timeout=None)
        self.sender:Player = sender
        self.address:str = "未選択"
        self.content:str = "メッセージ未入力"
        # HACK: このクラスがGameを知っているのはどうなの？（送信先のRoleを得るため)
        self.game = game
        
    @discord.ui.button(label="メッセージを入力する")
    async def input_callback(self,interaction:discord.Interaction,button:Button):
        interaction.response.send_modal(InputModal())
        
    # ここのroles_nameをgameからの参照に変えることはできない(デコレータではselfの参照ができないので)
    @discord.ui.select(options=list(map(lambda x:discord.SelectOption(label=x), roles_name)),
                       placeholder="宛先を選択")
    async def select_callback(self,interaction:discord.Interaction,select:Select):
        self.address = select.values[0]
        await interaction.response.edit_message(embed=self.GenerateInputStatus(),view=self)
        
    #HACK: 送信先が選択されるまでdisableにしたい/selectのcallback関数からアクセスする方法がわからない
    @discord.ui.button(label="送信する")
    async def button_callback(self,interaction:discord.Interaction,button:Button):
        # 送信先が未選択 or メッセージ未記入 ならスルー
        if self.address == "未選択": return
        if self.content == "未入力": return
        
        # 送信する
        # プレイヤーが2名いる状態の「クイーン」に送ると複数ログが出る
        for name,role in self.game.Roles:
            if name==self.address:
                if await self.sender.SendMessage(self.address,self.content):
                    await self.game.Players[self.game.Roles[name].player_name].ReceiveMessage(self.sender.role_name,self.content)
        
    def GenerateInputStatus(self) -> discord.Embed:
        text = f"宛先: {self.address}\n\n{self.content}"
        embed = discord.Embed(title="メッセージ編集フォーム",color=0x7B68EE)
        embed.add_field(name='',value=text)
        return embed
    
    
# "メッセージを入力"するModal
class InputModal(discord.ui.Modal):
    ans = discord.ui.TextInput(label="メッセージ")
    def __init__(self,view:MessageInputForm):
        super().__init__(timeout=None)
        self.view:MessageInputForm = view
        
    async def on_submit(self, interaction: Interaction) -> None:
        self.view.content = self.ans.value
        await interaction.response.edit_message(embed=self.view.GenerateInputStatus(),view=self.view)
            
'''
    サーバーの識別
'''
        
# セーブデータが存在するなら読み込む。ないなら作る
# サーバーに対応するGameインスタンスを返す
async def VerifyGuild(message:discord.Message) -> Game:
    # 対応するGameインスタンスが存在する
    if message.guild in games: return games[message.guild]
    
    # 対応するGameインスタンスが存在しない 
    game = Game(message.channel)
    games[message.guild] = game
    with open('save_data.json') as f:
        try:
            data = json.load(f)
        except:
            data = dict()
        # セーブデータがあるなら、ロードする
        if str(message.guild.id) in data.keys():
            game.Load(data[str(message.guild.id)]) 
            if game.phase == "ゲーム進行中":
                # TODO: チャンネルが全て存在しているかチェック
                await SendSystemMessage(game.loby,headline="ゲームを再開します")
            if game.phase == "ゲーム終了":
                await SendSystemMessage(game.loby,headline="ゲームが既に終了しています",content="新規ゲームを始める場合は「!start」を入力してください")
        else:
            await SendSystemMessage(game.loby,headline="新規ゲームデータを作成しました")
            game.Save()
    return game

# TODO: ゲームデータを削除する関数
    
'''
    実行
'''
    
@client.event
async def on_ready():
    print("on_ready",discord.__version__)
@client.event
async def on_message(message:discord.Message):
    if message.author.bot: return
    game = await VerifyGuild(message)
    
    #コマンドの解釈・実行
    await game.Interpret(message)
    
client.run(TOKEN)
        