import json
import discord
import re
from discord.interactions import Interaction
from discord.ui import Select,View,Button
from discord.ext import tasks
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

# 名前の入力に対して、表記揺れから正規表現に直す
def DefineNameVariants(name_input:str) -> str:
    for name,player in player_data.items():
        if name_input in player["name_variants"]: 
            return name
    return False
    
# 役職名リスト（ラベル用）
#roles_name = [item for item in role_data]
'''
    Role, Player
'''
# 必ずPlayerに付随して生成される、ようにする？
class Role:
    def __init__(self,name:str,player_name:str):
        self.name:str = name
        self.player_name:str = player_name
        self.remaining_ability_usage:int = role_data[name]["ability_usage_count"] #能力使用回数
        self.is_ability_blocked:bool = False #エースの妨害を受けているか/能力入れ替えでリセット
        #self.answer_status = list()
        
    #ヘルプメッセージを(項目:本文)の辞書で返す
    #Spade2(能力の残り使用回数->無制限), Joker(脱出条件)はオーバーライドする
    def GetHelpMessage(self) -> dict:
        res = dict()
        res["役職"] = self.name
        res["能力の残り使用回数"] = self.remaining_ability_usage
        res["能力"] = role_data[self.name]["ability_description"]
        res["脱出条件"] = role_data[self.name]["escape_condition"]
        if self.is_ability_blocked: res["備考"] = "能力の使用が妨害されている"
        return res
    
    # 能力のある役職はオーバーライドする
    # 発動条件を確認する 阻害されていると、使用回数を消費してエラーを返す
    async def UseAbility(self,player,game):
        if self.remaining_ability_usage <= 0:
            raise Exception('能力の使用可能回数が残っていません')
        if self.is_ability_blocked:
            self.remaining_ability_usage -= 1
            self.is_ability_blocked = False
            raise Exception('能力の使用が妨害されています')
    
class Player:
    def __init__(self,player_name:str,role:Role):
        self.player_name:str = player_name
        self.role:Role = role
        self.channel:discord.TextChannel = None
        self.sendable_roles = [item for item in role_data]
        self.replyable_roles = list()
        self.waiting_embed:discord.Message = None #入力待ち中にコマンドを入力されたときに処理を中断する用
        
    #ヘルプメッセージを(項目:本文)の辞書で返す
    async def PrintHelpMessage(self,game):
        res = {"あなたの名前":self.player_name,"ゲーム経過時間":f'{game.time_in_game}分'}
        res.update(self.role.GetHelpMessage())
        res["あなたの名前"] = self.player_name
        res["DMを送信可能"] = ','.join(self.sendable_roles)
        res["返信を送信可能"] = ','.join(self.replyable_roles)
        
        embed = discord.Embed(title='Help')
        #information = ''
        for key,value in res.items():
            #information += f'{key}:    {value}\n'
            if key in ['DMを送信可能','返信を送信可能','能力','脱出条件']: embed.add_field(name=key,value=value,inline=False)
            else: embed.add_field(name=key,value=value)
        #embed.add_field(name='情報',value=information)
        cmd = '!help    ヘルプを表示\n!dm    DMを入力\n!reply    返信を入力\n!use    能力を持つ場合、発動する'
        embed.add_field(name='コマンド',value=cmd,inline=False)
        await self.channel.send(embed=embed)
    
    #現在実行中のView(Button,Select...)を中止し、エラーメッセージに差し替える
    async def CancelView(self):
        #待機中のViewがなければそのまま
        if self.waiting_embed==None: return
        #if self.waiting_embed==None: raise Exception("No active process found.")
        
        await self.waiting_embed.edit(embed=GetErrorEmbed("中断しました"))
        self.waiting_embed = None
        
    #「DM」を送るためのフォーム
    # Mikado(特定個人にしか送れない)、Doki(Jokerに無制限に遅れる)はオーバーライドする
    async def SendMessageInputForm(self,game):
        # Error: 既に上限までメッセージを送信した
        if not self.sendable_roles:
            await SendError(self.channel,'送信可能な宛先がありません')
            return
        
        # 入力フォームを送信
        view = MessageInputForm(game,self) # embed: 入力内容を表示    view: 入力ボタン、送信先選択、送信ボタン
        for role_name in self.sendable_roles:
            view.select_callback.add_option(label=role_name)
        msg = await self.channel.send(embed=view.GenerateInputStatus(),view=view)
        self.waiting_embed = msg
        
    async def SendReplyInputForm(self,game):
        # Error: 送信可能な役職がない
        if not self.replyable_roles:
            await SendError(self.channel,'返信可能な宛先がありません')
            return
        
        view = MessageInputForm(game,self,is_reply=True) 
        for role_name in self.replyable_roles:
            view.select_callback.add_option(discord.SelectOption(label=role_name))
        msg = await self.channel.send(embed=view.GenerateInputStatus(),view=view)
        self.waiting_embed = msg
    
    # メッセージを送信可能か判定し、送信ステータスを更新する
    # 入力フォームから呼ばれる
    def SendMessage(self,address_role:str,content:str,is_reply:bool=False):
        if is_reply:
            if not address_role in self.replyable_roles:
                raise Exception('invalid address')
            self.replyable_roles.remove(address_role)
        else:
            if not address_role in self.sendable_roles:
                raise Exception('invalid address')
            self.sendable_roles.remove(address_role)
    
    # 受信側
    async def ReceiveMessage(self,sender_role:str,content:str,is_reply:bool=False):
        if is_reply:
            await self.channel.send(embed=discord.Embed(title=f'{sender_role}から返信が届きました',description=content,color=0x7B68EE))
        else:
            await self.channel.send(embed=discord.Embed(title=f'{sender_role}からメッセージが届きました',description=f'(!reply で返信できます)\n\n{content}',color=0x7B68EE))
            self.replyable_roles.append(sender_role)
 
'''
    Role, Player の派生クラス
'''
class Ace(Role):
    # Halt: 名前を入力した一人の能力発動を一度だけ空打ちさせる
    async def UseAbility(self,player:Player,game):
        await player.CancelView()
        channel = player.channel
        try:
            await super().UseAbility(player,game)
        except Exception as e:
            await SendError(player.channel,e)
            return
        msg = await SendSystemMessage(channel,'対象の名前を入力してください')
        try:
            res = await WaitForResponse(channel)
        except:
            msg.edit(embed=GetErrorEmbed('中断しました'))
            return
        res = DefineNameVariants(res)
        if res=="帝秀一" or not res:
            await SendError(channel,'対象の人物は選択できません')
            return
        # 入力後、もういちど条件チェック
        try:
            await super().UseAbility(player,game)
        except Exception as e:
            await SendError(player.channel,e)
            return
        game.Players[res].role.is_ability_blocked = True
        self.remaining_ability_usage -= 1
        await SendSystemMessage(channel,f'{res}の能力使用を妨害しています...')
class Club3(Role):
    # Swap: 入力した二人の能力を入れ替え、使用回数と妨害状況をリセットする
    async def UseAbility(self,player:Player,game):
        await player.CancelView()
        try:
            await super().UseAbility(player,game)
        except Exception as e:
            await SendError(player.channel,e)
            return
        target = list()
        # 2人の名前を入力させる(選択肢を知らせないため、手打ちしてもらう)
        for i in range(2):
            msg = await SendSystemMessage(player.channel,f'{i}人目の名前を入力してください')
            try:
                res = await WaitForResponse(player.channel)
            except:
                msg.edit(embed=GetErrorEmbed('中断しました'))
                return
            res = DefineNameVariants(res)
            if res=="帝秀一" or not res:
                await SendError(player.channel,'対象の人物は選択できません')
                return
            elif res==player.player_name:
                await SendError(player.channel,'あなた自身は選択できません')
                return
            target.append(res)
        if target[0]==target[1]:
            await SendError(player.channel,'デバッグしようとしていますか？')
            return
        # 入力後の再チェック
        try:
            await super().UseAbility(player,game)
        except Exception as e:
            await SendError(player.channel,e)
            return
        
        # Swap
        await game.Players[target[0]].CancelView()
        await game.Players[target[1]].CancelView()
        temp = game.Players[target[0]].role.name
        game.Players[target[0]].role = GetRole(game.Players[target[1]].role.name,target[0])
        game.Players[target[1]].role = GetRole(temp,target[1])
        # 変更通知
        await SendSystemMessage(game.Players[target[0]].channel,'あなたの役職が変更されました\¥n!help で確認してください')
        await SendSystemMessage(game.Players[target[1]].channel,'あなたの役職が変更されました\¥n!help で確認してください')
        await SendSystemMessage(player.channel,f'{target[0]}と{target[1]}の役職を入れ替えました')
        player.role.remaining_ability_usage -= 1
'''
    ユーザーとのやり取りなど
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

# 指定したロールの派生クラスを返す　なかったら素のロール
def GetRole(role_name:str,player_name:str):
    if role_name=='エース': return Ace(role_name,player_name)
    elif role_name=='クラブの３': return Club3(role_name,player_name)
    return Role(role_name,player_name)

'''
    ゲーム
'''
class Game:
    def __init__(self,loby:discord.TextChannel):
        # lobyは接続時に最初に発言したチャンネルに固定する(以前のチャンネル全てが削除されてやり取り不可になるのを避けるため)
        self.loby:discord.TextChannel = loby
        
        self.admin:discord.TextChannel = None
        self.Players:dict = dict() # player_name -> Player
        self.Roles:dict = dict() # role_name -> List[Player]
        # "ゲーム開始前" -> "ゲーム進行中" -> "ゲーム終了"
        self.phase = "ゲーム開始前"
        self.time_in_game = 0
        
        for name,player in player_data.items():
            # TODO: 帝のRoleを作ったら書き換える
            if name=="帝秀一":
                self.Players[name] = Player(name,None)
                continue
            self.Players[name] = Player(name,GetRole(player["initial_role"],name))
            # Role項目の存在確認
            if player["initial_role"] in self.Roles: self.Roles[player["initial_role"]].append(self.Players[name])
            else: self.Roles[player["initial_role"]] = [self.Players[name]]
          
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
        
        # 各Playerのデータ
        players = dict()
        for player in self.Players.values():
            d = dict()
            if player.role:
                d["role_name"] = player.role.name
                d["remaining_ability_usage"] = player.role.remaining_ability_usage
                d["is_ability_blocked"] = player.role.is_ability_blocked
            if player.channel:
                d["channel_id"] = player.channel.id
            else:
                d["channel_id"] = None
            d["sendable_roles"] = player.sendable_roles
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
        
        # Roles初期化
        self.Roles = dict()
        for player_name,data in guild_data["players"].items():
            if data["channel_id"]: self.Players[player_name].channel = client.get_channel(data["channel_id"])
            self.Players[player_name].sendable_roles = data["sendable_roles"]
            self.Players[player_name].replyable_roles = data["replyable_roles"]
            
            # ロールなし
            if player_name=='帝秀一': continue
            
            if data["role_name"] in self.Roles: self.Roles[data["role_name"]].append(self.Players[player_name])
            else: self.Roles[data["role_name"]] = [self.Players[player_name]]
            # ロールの初期化
            self.Players[player_name].role = GetRole(data["role_name"],player_name)
            # ロールの設定
            self.Players[player_name].role.remaining_ability_usage = data["remaining_ability_usage"]
            self.Players[player_name].role.is_ability_blocked = data["is_ability_blocked"]
          
    
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
        for person in self.Players.values():
            if message.channel==person.channel: author:Player = person
        if not author: return
        
        if type(author)==Player:
            for person in self.Players.values():
                if message.channel==person.channel: player:Player = person
                
            # 本来はゲーム中コマンド
            # プレイヤー用コマンド
            if cmd=="dm" or cmd=="DM": await player.SendMessageInputForm(self)
            if cmd=="use": await player.role.UseAbility(player,self)
            if cmd=="help": await player.PrintHelpMessage(self)
            
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
        try:
            res = await WaitForResponse(channel)
        except:
            await SendError(channel,'中断しました')
            return
        if res=="admin" or res=="Admin" or res=="ADMIN":
            self.admin = channel
            await SendSystemMessage(channel,"adminチャンネルを設定しました")
            return
        if res=="loby" or res=="Loby" or res=="LOBY":
            self.loby = channel
            await SendSystemMessage(channel,"lobyを設定しました")
            return
        name = DefineNameVariants(res)
        if name:
            self.Players[name].channel = channel
            await SendSystemMessage(channel,f"{name}のチャンネルを設定しました")
            return
        await SendError(channel,"該当のチャンネル名が見つかりません\n漢字、ひらがな、名字、名前、フルネームのいずれかで入力してください")
        
    # チャンネルが全て設定済みかどうか
    # True or (未設定のチャンネル名リスト) を返す
    def IsChannelReady(self):
        unset_channels = list()
        if not self.loby: unset_channels.append("loby")
        if not self.admin: unset_channels.append("admin")
        for player in self.Players.values():
            if not player.channel: unset_channels.append(player.player_name)
            
        if unset_channels: return unset_channels
        else: return True
        
'''
    discord.ui
'''

# メッセージ入力フォーム: 
class MessageInputForm(View):
    def __init__(self,game:Game,sender:Player,is_reply:bool=False):
        super().__init__(timeout=None)
        self.sender:Player = sender
        self.address:str = "未選択"
        self.content:str = "メッセージ未入力"
        self.is_reply = is_reply
        # HACK: このクラスがGameを知っているのはどうなの？（送信先のRoleを得るため)
        self.game = game
        
    @discord.ui.button(label="メッセージを入力する")
    async def input_callback(self,interaction:discord.Interaction,button:Button):
        await interaction.response.send_modal(InputModal(self))
        
    # 選択肢は可変なので、外からadd_optionで渡す
    @discord.ui.select(placeholder="宛先を選択")
    async def select_callback(self,interaction:discord.Interaction,select:Select):
        self.address = select.values[0]
        await interaction.response.edit_message(embed=self.GenerateInputStatus(),view=self)
        
    #HACK: 送信先が選択されるまでdisableにしたい/selectのcallback関数からアクセスする方法がわからない
    @discord.ui.button(label="送信する")
    async def button_callback(self,interaction:discord.Interaction,button:Button):
        # 送信先が未選択 or メッセージ未記入 ならスルー
        if self.address == "未選択" or self.content == "メッセージ未入力":
            await interaction.response.send_message(embed=GetErrorEmbed('未入力の項目があります'))
            return
        
        # 送信できるか確認
        try: 
            self.sender.SendMessage(self.address,self.content,self.is_reply)
        except Exception:
            await interaction.response.send_message(embed=GetErrorEmbed('送信できない宛先です'))
            return
        # 実行
        for player in self.game.Players.values():
            if player.role.name==self.address: await player.ReceiveMessage(self.sender.role_name,self.content,self.is_reply)
        await interaction.response.edit_message(view=None,embed=discord.Embed(title=f'以下のメッセージを送信しました',description=f'{self.sender.role.name}からメッセージが届きました\n\n{self.content}'))
        
    def GenerateInputStatus(self) -> discord.Embed:
        text = f"宛先: {self.address}\n\n{self.content}"
        embed = discord.Embed(title="メッセージ編集フォーム",color=0x7B68EE)
        embed.add_field(name='',value=text)
        return embed
    
    
# "メッセージを入力"するModal
class InputModal(discord.ui.Modal,title='入力フォーム'):
    ans = discord.ui.TextInput(label="メッセージ本文",style=discord.TextStyle.paragraph)
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
# 進行中のゲームの時間を進める
# 起動直後にも呼ばれるので、即座に経過時間1分になることに注意
@tasks.loop(minutes=1)
async def loop():
    for game in games.values():
        if game.phase=="ゲーム進行中":
            game.time_in_game += 1
            # TODO: あとで書く 帝のメッセージと、能力解禁
            if game.time_in_game in [10,15,20,30]: return
            elif game.time_in_game==90: return

@client.event
async def on_ready():
    print("on_ready",discord.__version__)
    loop.start() # 時間計測ループ
@client.event
async def on_message(message:discord.Message):
    if message.author.bot: return
    # サーバーの認証
    game = await VerifyGuild(message)
    # コマンドの解釈・実行
    await game.Interpret(message)
  
client.run(TOKEN) # イベントループ