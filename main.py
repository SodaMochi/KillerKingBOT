import json
import discord
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
with open('text_data.json') as f:
    text_data = json.load(f)
    
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
        if role_data[self.name]['ability_cooldown']>game.time_in_game:
            raise Exception('まだ能力が使用可能な時間ではありません')
        if self.remaining_ability_usage <= 0:
            raise Exception('能力の使用可能回数が残っていません')
        if self.is_ability_blocked:
            raise Exception('能力の使用が妨害されています')
    
class Player:
    def __init__(self,player_name:str,role:Role):
        self.player_name:str = player_name
        self.role:Role = role
        self.channel:discord.TextChannel = None
        self.sendable_roles = [item for item in role_data]
        self.replyable_roles = list()
        self.vital = 'alive' # or 'dead'
        self.waiting_embed:discord.Message = None #入力待ち中にコマンドを入力されたときに処理を中断する用
        
    #ヘルプメッセージを(項目:本文)の辞書で返す
    async def PrintHelpMessage(self,game):
        res = {"あなたの名前":self.player_name,"ゲーム経過時間":f'{game.time_in_game}分'}
        res.update(self.role.GetHelpMessage())
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
        if self.role.name=="クイーン" or self.role.name=="ジョーカー": cmd += '\n!answer ゲーム中に回答すべき質問に答える'
        embed.add_field(name='コマンド',value=cmd,inline=False)
        await self.channel.send(embed=embed)
    
    #現在実行中のView(Button,Select...)を中止し、エラーメッセージに差し替える
    async def CancelView(self):
        #待機中のViewがなければそのまま
        if self.waiting_embed==None: return
        #if self.waiting_embed==None: raise Exception("No active process found.")
        
        await self.waiting_embed.edit(embed=GetErrorEmbed("中断しました"),view=None)
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
        self.waiting_embed = await self.channel.send(embed=view.GenerateInputStatus(),view=view)
        
    async def SendReplyInputForm(self,game):
        # Error: 送信可能な役職がない
        if not self.replyable_roles:
            await SendError(self.channel,'返信可能な宛先がありません')
            return
        
        view = MessageInputForm(game,self,is_reply=True) 
        for role_name in self.replyable_roles:
            view.select_callback.add_option(label=role_name)
        self.waiting_embed = await self.channel.send(embed=view.GenerateInputStatus(),view=view)
    
    # メッセージを送信可能か判定し、送信ステータスを更新する
    # 入力フォームから呼ばれる
    def SendMessage(self,address_role:str,is_reply:bool=False):
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
# 土岐いちか
class Doki(Player):
    async def SendMessage(self, address_role: str,is_reply: bool = False):
        if is_reply:
            if address_role not in self.replyable_roles:
                raise Exception('invalid address')
            self.replyable_roles.remove(address_role)
        else:
            if address_role not in self.sendable_roles:
                raise Exception('invalid address')
            if address_role!='ジョーカー':
                self.sendable_roles.remove(address_role)
# 帝
class Mikado(Player):
    def __init__(self,player_name:str,role:Role):
        super().__init__(player_name,role)
        self.sendable_roles = ['不明な宛先']
        
    async def PrintHelpMessage(self, game):
        res = {"あなたの名前":self.player_name,"ゲーム経過時間":f'{game.time_in_game}分'}
        #res.update(self.role.GetHelpMessage())
        res["DMを送信可能"] = ','.join(self.sendable_roles)
        
        embed = discord.Embed(title='Help')
        for key,value in res.items():
            if key in ['DMを送信可能','返信を送信可能','能力','脱出条件']: embed.add_field(name=key,value=value,inline=False)
            else: embed.add_field(name=key,value=value)
        cmd = '!help    ヘルプを表示\n!dm    DMを入力'
        embed.add_field(name='コマンド',value=cmd,inline=False)
        await self.channel.send(embed=embed)
        return super().PrintHelpMessage(game)
    def SendMessage(self,address_role:str,is_reply:bool=False):
        if len(self.sendable_roles)<1:
            raise Exception('invalid address')
        self.sendable_roles = list()

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
            await msg.edit(embed=GetErrorEmbed('中断しました'))
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
        for i in (1,2):
            msg = await SendSystemMessage(player.channel,f'{i}人目の名前を入力してください')
            try:
                res = await WaitForResponse(player.channel)
            except:
                await msg.edit(embed=GetErrorEmbed('中断しました'))
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
        game.Players[target[0]].role = NewRole(game.Players[target[1]].role.name,target[0])
        game.Players[target[1]].role = NewRole(temp,target[1])
        # 変更通知
        await SendSystemMessage(game.Players[target[0]].channel,'あなたの役職が変更されました\n!help で確認してください')
        await SendSystemMessage(game.Players[target[1]].channel,'あなたの役職が変更されました\n!help で確認してください')
        await SendSystemMessage(player.channel,f'{target[0]}と{target[1]}の役職を入れ替えました')
        self.remaining_ability_usage -= 1
class Queen(Role):
    def __init__(self,name,player_name:str):
        super().__init__(name,player_name)
        self.answer_status = {'ジャック':'未入力',
                              'クイーン':'制限時間内に全役職の脱出条件を特定する',
                              'キング':'未入力',
                              'エース':'未入力',
                              'スペードの２':'未入力',
                              'クラブの３':'未入力',
                              'ジョーカー':'未入力'}
    
    # FIXME: ここで設定したwaiting_embedが関数外に出るとすぐNoneになる
    async def Answer(self,game,player:Player):
        if player.waiting_embed: await player.CancelView()
        view = ViewForQueen(game,player)
        player.waiting_embed = await player.channel.send(view=view,embed=view.GenerateInputStatus())
    
    async def UseAbility(self, player:Player, game):
        await player.CancelView()
        try:
            await super().UseAbility(player,game)
        except Exception as e:
            await SendError(player.channel,e)
            return
        msg = await SendSystemMessage(player.channel,'対象の名前を入力してください')
        try:
            res = await WaitForResponse(player.channel)
        except:
            await msg.edit(embed=GetErrorEmbed('中断しました'))
            return
        res = DefineNameVariants(res)
        if res=="帝秀一" or not res:
            await SendError(player.channel,'対象の人物は選択できません')
            return
        #もういちどチェック
        try:
            await super().UseAbility(player,game)
        except Exception as e:
            await SendError(player.channel,e)
            return
        embed = discord.Embed(title=f'{res}の情報')
        embed.add_field(name='役職',value=game.Players[res].role.name,inline=False)
        if game.Players[res].role.name=='ジョーカー': embed.add_field(name='脱出条件',value=game.joker_escape_condition,inline=False)
        else: embed.add_field(name='脱出条件',value=role_data[game.Players[res].role.name]['escape_condition'],inline=False)
        await player.channel.send(embed=embed)
        self.remaining_ability_usage -= 1
class Spade2(Role):
    def GetHelpMessage(self) -> dict:
        return super().GetHelpMessage().update({'能力の残り使用回数':'無制限'})
    
    async def UseAbility(self, player:Player, game):
        await player.CancelView()
        try:
            await super().UseAbility(player,game)
        except Exception as e:
            await SendError(player.channel,e)
            return
        # 入力フォームを送信
        view = AnswerInputForm(game,player,['対象の名前','役職','脱出条件'],f'{self.name}({player.player_name})の殺害リクエスト')
        player.waiting_embed = await player.channel.send(embed=view.GenerateInputStatus(),view=view)
class King(Role):
    async def UseAbility(self, player:Player, game):
        await player.CancelView()
        try:
            await super().UseAbility(player,game)
        except Exception as e:
            await SendError(player.channel,e)
            return
        await SendSystemMessage(player.channel,'対象の名前を入力してください')
        msg = await SendSystemMessage(player.channel,'対象の名前を入力してください')
        try:
            res = await WaitForResponse(player.channel)
        except:
            await msg.edit(embed=GetErrorEmbed('中断しました'))
            return
        res = DefineNameVariants(res)
        if not res: #自己解釈: 帝も対象になる
            await SendError(player.channel,'不正な入力です')
            return
        # ダブルチェック
        try:
            await super().UseAbility(player,game)
        except Exception as e:
            await SendError(player.channel,e)
            return
        # 実行
        await SendSystemMessage(game.admin,headline='キングからの殺害要請',description=f'対象:{res}')
        await SendSystemMessage(player.channel,f'リクエストを送信しました\n対象:{res}')
        self.remaining_ability_usage -= 1
class Jack(Role):
    async def UseAbility(self, player:Player, game):
        await player.CancelView()
        try:
            await super().UseAbility(player,game)
        except Exception as e:
            await SendError(player.channel,e)
            return
        embed = discord.Embed(description=f'残り回数:{self.remaining_ability_usage}')
        embed.add_field(name='選択項目',value='\n'.join(text_data['Jack_Ability']))
        player.waiting_embed = await player.channel.send(view=ViewForJack(player),embed=embed)
class Joker(Role):
    def __init__(self,name:str,player_name:str):
        super().__init__(name,player_name)
        self.escape_condition:str = None
        self.answer_status = {'ジャック':'未入力',
                              'クイーン':'制限時間内に全役職の脱出条件を特定する',
                              'キング':'未入力',
                              'エース':'未入力',
                              'スペードの２':'未入力',
                              'クラブの３':'未入力',
                              'ジョーカー':'未入力'}
    
    async def Answer(self,game,player:Player):
        view = ViewForQueen(game,player)
        player.waiting_embed = await player.channel.send(view=view,embed=view.GenerateInputStatus())
    
    async def UseAbility(self, player:Player, game):
        await SendError(player.channel,'不正なコマンドです')
        
    def GetHelpMessage(self) -> dict:
        d = super().GetHelpMessage()
        d['脱出条件'] = self.escape_condition
        return d
    
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
async def SendSystemMessage(textchannel:discord.TextChannel,description='',headline='',content='',mention:str=None):
    embed =  discord.Embed(title='System Message',description=description,color=0x4169E1)
    if content or headline: embed.add_field(name=headline,value=content)
    if mention: return await textchannel.send(content=mention,embed=embed)
    else: return await textchannel.send(embed=embed)

# 指定のテキストチャンネルに「Error」を送る（ゲーム上の演出）
# 後で編集可能にするため、返り値として送信メッセージのインスタンスを返す
async def SendError(textchannel:discord.TextChannel,content:str):
    embed = discord.Embed(title='Error',description=content,color=0xFF0000)
    return await textchannel.send(embed=embed)
# embedのみ返す版(edit_message用)
def GetErrorEmbed(content:str):
    return discord.Embed(title='Error',description=content,color=0xFF0000)

# 指定したロールの派生クラスを返す　なかったら素のロール
def NewRole(role_name:str,player_name:str):
    if role_name=='エース': return Ace(role_name,player_name)
    elif role_name=='クラブの３': return Club3(role_name,player_name)
    elif role_name=='クイーン': return Queen(role_name,player_name)
    elif role_name=='スペードの２': return Spade2(role_name,player_name)
    elif role_name=='キング': return King(role_name,player_name)
    elif role_name=='ジャック': return Jack(role_name,player_name)
    elif role_name=='ジョーカー': return Joker(role_name,player_name)
    return Role(role_name,player_name)
def NewPlayer(name:str,role:Role):
    if name=='帝秀一': return Mikado(name,role)
    elif name=='土岐いちか': return Doki(name,role)
    return Player(name,role)

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
        #self.joker_escape_condition:str = None
        
        for name,player in player_data.items():
            if name=="帝秀一":
                self.Players[name] = Mikado(name,None)
                continue
            self.Players[name] = NewPlayer(name,NewRole(player["initial_role"],name))
            if name=="岩井紅音": self.Players[name].vital = "dead"
            # Rolesにも追加
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
        save["joker_escape_condition"] = self.Roles['ジョーカー'][0].role.escape_condition
        
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
            d["vital"] = player.vital
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
            self.Players[player_name].vital = data["vital"]
            
            # ロールなし
            if player_name=='帝秀一': continue
            
            if data["role_name"] in self.Roles: self.Roles[data["role_name"]].append(self.Players[player_name])
            else: self.Roles[data["role_name"]] = [self.Players[player_name]]
            # ロールの初期化
            self.Players[player_name].role = NewRole(data["role_name"],player_name)
            # ロールの設定
            self.Players[player_name].role.remaining_ability_usage = data["remaining_ability_usage"]
            self.Players[player_name].role.is_ability_blocked = data["is_ability_blocked"]
        self.Roles['ジョーカー'][0].role.escape_condition = guild_data["joker_escape_condition"]
          
    async def StartGame(self):
        if self.phase!='ゲーム開始前': return
        # チャンネル設定の確認
        check = self.IsChannelReady()
        if not check==True:
            await SendError(self.admin,f'{", ".join(check)}が未設定です')
            return
        
        # ジョーカーの脱出条件を設定
        if not self.Roles['ジョーカー'][0].role.escape_condition:
            await self.admin.send(content='ジョーカーのコピー先を選択してください',view=ViewForJoker(self))
            return
        
        self.phase = 'ゲーム進行中'
        await SendSystemMessage(self.loby,headline='ゲームを開始します',content='ゲーム内コマンドが使用可能になりました\n!help で確認してください',mention='@here')
          
    async def EndGame(self):
        if self.time_in_game<120:
            msg = await SendSystemMessage(self.admin,f'現在ゲーム開始から{self.time_in_game}分です。\n本当にゲームを終了させる場合は「yes」を送信してください')
            try:
                res = WaitForResponse(self.admin)
                if not (res=="yes" or res=="YES"): raise Exception()
            except:
                await msg.edit(embed=GetErrorEmbed('中断しました'))
        for player in self.Players.values():
            await player.CancelView()
            if player_data[player.player_name]["question"]:
                view = AnswerInputForm(self,player,player_data[player.player_name]["question"],
                                       title=f'{player.player_name}としての回答')
                await player.channel.send(view=view,embed=view.GenerateInputStatus())
            if player.role and player.channel:
                if role_data[player.role.name]["question"]:
                    view = AnswerInputForm(self,player,role_data[player.role.name]["question"],
                                       title=f'{player.role.name}({player.player_name})としての回答')
                    await player.channel.send(view=view,embed=view.GenerateInputStatus())
        self.phase = "ゲーム終了"
        await SendSystemMessage(self.loby,'ゲームを終了しました',mention='@here')
          
    async def PrintAdminHelp(self):
        in_game = {"kill":"プレイヤーを死亡させる",
                "change":"（トラブル対応用）DM送信状況や能力使用回数などを手動で変更する",
                "end":"ゲームを終了し、ゲーム終了時の質問に回答させる"}
        anytime = {"key":"プレイヤーに脱出パスワードを送信する",
                   "set":"手動でチャンネルを設定・変更する",
                   "save":"ゲームデータを保存する(BOTがオフラインになっても保存されます)",
                   "delete":"ゲームデータを削除する",
                   "help":"現在の状況、コマンドを確認する"}
        pre_game = {"allset":"すべてのチャンネル・ロールを作成する",
                    "start":"デスゲームを開始する"}
        embed = discord.Embed(title='Help')
        embed.add_field(name='現在の状況',value=self.phase)
        embed.add_field(name='ゲーム経過時間',value=self.time_in_game)
        embed.add_field(name='いつでも使えるコマンド',value='\n'.join([f'`!{key}    {value}`'for key,value in anytime.items()]),inline=False)
        if self.phase=='ゲーム進行中': embed.add_field(name='ゲーム中コマンド',value='\n'.join([f'`!{key}    {value}`'for key,value in in_game.items()]),inline=False)
        if self.phase=='ゲーム開始前': embed.add_field(name='ゲーム開始前コマンド',value='\n'.join([f'`!{key}    {value}`'for key,value in pre_game.items()]),inline=False)
        await self.admin.send(embed=embed)
          
    async def Kill(self):
        await SendSystemMessage(self.admin,'死亡者の名前を入力してください')
        res = await WaitForResponse(self.admin)
        res = DefineNameVariants(res)
        if not res:
            await SendError(self.admin,'不正な入力です')
            return
        self.Players[res].vital = 'dead'
        await SendSystemMessage(self.admin,f'{res}は死亡しました')
        await SendSystemMessage(self.Players[res].channel,'あなたは死亡しました')
        
    async def GivePassword(self):
        select = ViewForPassword(self)
        for player_name in self.Players.keys():
            if player_name=="岩井紅音" or player_name=="帝秀一": continue #パスワードを受け取る権利がない
            select.callback.add_option(label=player_name)
        await self.admin.send(content='パスワードの送信先を選んでください',view=select)
        
    async def TriggerTimedEvent(self):
        # 帝の思い出しメッセージ
        if self.time_in_game in [10,15,20,30]:
            await self.Players['帝秀一'].channel.send(embed=discord.Embed(description=text_data['Mikado_Memory'][str(self.time_in_game)]))
        # 終了30分前の能力解放
        if self.time_in_game==90:
            for player in self.Roles['スペードの２']+self.Roles['キング']:
                await SendSystemMessage(player.channel,'ゲーム終了30分前です\nあなたの能力が使用可能になりました。!useで使用できます')
        if self.time_in_game==120:
            await SendSystemMessage(self.loby,'ゲーム終了時間になりました。\nゲームマスターの指示にしたがってください',mention='@here')
            await SendSystemMessage(self.admin,'ゲーム終了時間になりました。ゲームを終了（スマホの能力を停止し、ゲーム終了時の質問に回答させる）する場合は!endを入力してください',mention='@here')
    
    async def ChangeParams(self):
        # options(key: value)に番号を振り当て、番号で回答させる
        # 選択したkeyのvalueを返す 失敗したらFalseを返す
        async def wait(options:dict):
            values = list()
            keys = ''
            for key,value in options.items():
                values.append(value)
                keys += f'\n{len(values)}. {key}'
            msg = await SendSystemMessage(self.admin,headline='番号を入力してください',content=keys)
            try:
                res = await WaitForResponse(self.admin)
                res = int(res)
                if res<1 or len(options)<res: raise Exception()
            except:
                await msg.edit(embed=GetErrorEmbed('中断しました'))
                return False
            return values[res-1]
        
        # 分岐をネストして書いたほうが見やすいかもと思ってそうしてみた
        options = {'ゲーム経過時間を変更':'time'}
        for name,player in self.Players.items():
            options[f'{name}の情報を変更'] = player
        res = await wait(options)
        if not res: return
        
        if res=='time': # 時間の変更
            msg = await SendSystemMessage(self.admin,headline='経過分数を入力してください')
            try:
                res = await WaitForResponse(self.admin)
                res = int(res)
                if res<0 or res>120: raise Exception()
            except:
                await msg.edit(embed=GetErrorEmbed('中断しました'))
                return
            self.time_in_game = res
            await SendSystemMessage(self.admin,f'ゲーム経過時間が{res}分に変更されました')
            await SendSystemMessage(self.loby,f'ゲーム経過時間が{res}分に変更されました')
            return
        
        player:Player = res
        if player.player_name=='帝秀一':
            options = {f'生存状況を変更(現在:{player.vital})':'生存状況',
                       f'DM送信状況({"未送信" if player.sendable_roles else "送信済み"})':'DM送信状況'}
            res = await wait(options)
            if not res: return
            if res=='生存状況':
                if player.vital=='dead': player.vital='alive'
                else: player.vital = 'dead'
                await SendSystemMessage(self.admin,f'{player.player_name}は{player.vital}になりました')
            elif res=='DM送信状況':
                if player.sendable_roles: player.sendable_roles = list()
                else: player.sendable_roles = ['不明な宛先']
                await SendSystemMessage(self.admin,f'変更後の送信状況:{"未送信" if player.sendable_roles else "送信済み"}')
        else: #その他一般プレイヤー
            options = {f'生存状況を変更(現在:{player.vital})':'生存状況',
                       f'DM送信可能な役職':'DM送信可能な役職',
                       f'返信可能な役職':'返信可能な役職',
                       f'能力の妨害状況(現在:{"妨害中" if player.role.is_ability_blocked else "なし"})':'能力の妨害状況',
                       f'能力の残り使用回数(現在:{player.role.remaining_ability_usage})':'能力の残り使用回数'}
            res = await wait(options)
            if not res: return
            if res=='生存状況':
                if player.vital=='dead': player.vital='alive'
                else: player.vital = 'dead'
                await SendSystemMessage(self.admin,f'{player.player_name}は{player.vital}になりました')
            elif res=='DM送信可能な役職':
                options = dict()
                for role_name in role_data:
                    options[f'{role_name}({"未送信" if role_name in player.sendable_roles else "送信済み"})'] = role_name
                res = await wait(options)
                if not res: return
                if res in player.sendable_roles: player.sendable_roles.remove(res)
                else: player.sendable_roles.append(res)
                await SendSystemMessage(self.admin,headline='変更の送信可能リスト',content=player.sendable_roles)
            elif res=='返信可能な役職':
                options = dict()
                for role_name in role_data:
                    options[f'{role_name}({"返信可能" if role_name in player.replyable_roles else "返信不可"})'] = role_name
                res = await wait(options)
                if not res: return
                if res in player.replyable_roles: player.replyable_roles.remove(res)
                else: player.replyable_roles.append(res)
                await SendSystemMessage(self.admin,headline='変更の返信可能リスト',content=player.replyable_roles)
            elif res=='能力の妨害状況':
                player.role.is_ability_blocked = not player.role.is_ability_blocked
                await SendSystemMessage(self.admin,f'変更後: {"妨害中" if player.role.is_ability_blocked else "なし"}')
            elif res=='能力の残り使用回数':
                msg = await SendSystemMessage(self.admin,headline='変更後の能力の残り使用回数を入力してください')
                try:
                    res = await WaitForResponse(self.admin)
                    res = int(res)
                    if res<0: raise Exception()
                except:
                    await msg.edit(embed=GetErrorEmbed('中断しました'))
                    return
                player.role.remaining_ability_usage = res
                await SendSystemMessage(self.admin,f'変更後: {player.role.remaining_ability_usage}回')
            
    async def Interpret(self,message:discord.Message):
        #if not message.content.startswith("!"): return
        cmd = message.content[1:]
        
        if cmd=="set":
            await self.SetChannel(message.channel)
        elif cmd=="allset": await self.SetAllChannel(message)
        elif cmd=="save":
            self.Save()
            await SendSystemMessage(message.channel,'進行状況を保存しました')
        elif cmd=="delete":
            await DeleteGameData(self,message)
        elif cmd=='start': await self.StartGame()
        
        author = ""
        if message.channel==self.loby: author = "loby"
        elif message.channel==self.admin: author = "admin"
        for person in self.Players.values():
            if message.channel==person.channel: author:Player = person
        # 以降、全てのチャンネル設定が前提
        if not author: return
        
        # lobyコマンド
        if author=='loby': return
        
        # adminコマンド
        if author=='admin':
            if cmd=='help': await self.PrintAdminHelp()
            elif cmd=='key': await self.GivePassword()
            
            # ゲーム中
            if self.phase!="ゲーム進行中": return
            if cmd=='kill': await self.Kill()
            elif cmd=='change': await self.ChangeParams()
            elif cmd=='end': await self.EndGame()
            
            return
        
        # ゲーム中、プレイヤー
        if self.phase=='ゲーム進行中':
            for person in self.Players.values():
                if message.channel==person.channel: player:Player = person
            # 生存確認
            if player.vital!='alive': return
                
            # 帝秀一
            if player.player_name=='帝秀一':
                if cmd=='help': await player.PrintHelpMessage(self)
                if cmd=='dm': await player.SendMessageInputForm(self)
                return
            # 本来はゲーム中コマンド
            # プレイヤー用コマンド
            if cmd=="dm" or cmd=="DM": await player.SendMessageInputForm(self)
            if cmd=="reply": await player.SendReplyInputForm(self)
            if cmd=="use": await player.role.UseAbility(player,self)
            if cmd=="help": await player.PrintHelpMessage(self)
            if cmd=="answer":
                if player.role.name=='クイーン' or (player.role.name=='ジョーカー' and self.Roles['ジョーカー'][0].role.escape_condition.startswith('制限時間内に全役職の脱出条件を特定する')):
                    await player.role.Answer(self,player)
        self.Save() #セーブ
            
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
      
    async def SetAllChannel(self,message:discord.Message):
        if self.IsChannelReady()==True:
            msg = await SendSystemMessage(message.channel,"すでに全てのチャンネルが設定済みです。本当に実行する場合は「yes」を入力してください")
            try:
                res = await WaitForResponse(message.channel)
                if res!="yes": raise Exception()
            except Exception:
                await msg.edit(embed=GetErrorEmbed('中断しました'))
                return
        # 該当ロールが無ければ作成 (海山月正ロールを作るとネタバレなので、GM_KillerKingで代用)
        names = list(map(lambda role:role.name, message.guild.roles)) #discordギルドのロール名リスト
        if 'GM_KillerKing' in names: #あるなら取得
            admin_role = message.guild.roles[names.index("GM_KillerKing")]
        else:
            admin_role = await message.guild.create_role(name='GM_KillerKing')
        admin_overwrites = {message.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                            admin_role: discord.PermissionOverwrite(read_messages=True),
                            message.guild.me: discord.PermissionOverwrite(read_messages=True)}
        self.admin = await message.guild.create_text_channel(name='admin',category=message.channel.category,
                                                overwrites=admin_overwrites)
        for player_name in player_data:
            if player_name=="岩井紅音": continue
            if player_name=="海山月正": #GM_KillerKing ロールを適用（ネタバレ防止）
                self.Players[player_name].channel = await message.guild.create_text_channel(name=f'{player_name}のスマホ',overwrites=admin_overwrites)
                continue
            if player_name in names:
                role = message.guild.roles[names.index(player_name)]
            else:
                # 新規作成
                role = await message.guild.create_role(name=player_name)
            # チャンネル作成
            self.Players[player_name].channel = await message.guild.create_text_channel(name=f'{player_name}のスマホ',
                        overwrites={message.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                        admin_role: discord.PermissionOverwrite(read_messages=True),
                        role: discord.PermissionOverwrite(read_messages=True),
                        message.guild.me: discord.PermissionOverwrite(read_messages=True)},category=message.channel.category)
        await SendSystemMessage(self.loby,headline='チャンネル・ロールを作成しました',content='プレイヤーに各ロールを割り当て、プロローグを終えたら!startでゲームを開始してください')

            
        
    # チャンネルが全て設定済みかどうか
    # True or (未設定のチャンネル名リスト) を返す
    def IsChannelReady(self):
        unset_channels = list()
        if not self.loby: unset_channels.append("loby")
        if not self.admin: unset_channels.append("admin")
        for player in self.Players.values():
            if player.player_name=='岩井紅音': continue
            if not player.channel: unset_channels.append(player.player_name)
            
        if unset_channels: return unset_channels
        else: return True
        
'''
    discord.ui
'''
# メッセージ入力フォーム
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
        self.sender.waiting_embed = None
        
        # 送信できるか確認
        try: 
            self.sender.SendMessage(self.address,self.is_reply)
        except Exception:
            await interaction.response.send_message(embed=GetErrorEmbed('送信できない宛先です'))
            return
        # 実行
        if self.address=='帝秀一': #プレイヤーがジョーカーでり、帝秀一からのメッセージに返信する場合
            self.game.Players['帝秀一'].ReceiveMessage('不明な宛先',self.content,self.is_reply)
            await interaction.response.edit_message(view=None,embed=discord.Embed(title=f'以下のメッセージを送信しました',description=f'返信が届きました\n\n{self.content}'))
            return
        for player in self.game.Players.values():
            if player.player_name=='帝秀一': continue
            if player.role.name==self.address: await player.ReceiveMessage(self.sender.role.name,self.content,self.is_reply)
        if self.sender.player_name=='帝秀一':
            await self.game.Roles['ジョーカー'][0].ReceiveMessage("帝秀一",self.content,self.is_reply)
            await interaction.response.edit_message(view=None,embed=discord.Embed(title=f'以下のメッセージを送信しました',description=f'{self.sender.player_name}から{"返信" if self.is_reply else "メッセージ"}が届きました\n\n{self.content}'))
        else: await interaction.response.edit_message(view=None,embed=discord.Embed(title=f'以下のメッセージを{self.address}に送信しました',description=f'{self.sender.role.name}から{"返信" if self.is_reply else "メッセージ"}が届きました\n\n{self.content}'))
        
    def GenerateInputStatus(self) -> discord.Embed:
        text = f"宛先: {self.address}\n\n{self.content}"
        embed = discord.Embed(title="メッセージ編集フォーム",color=0x7B68EE)
        embed.add_field(name='',value=text)
        return embed
    
# 帝用メッセージ入力フォーム
class MikadoInputForm(View):
    def __init__(self,game:Game):
        super().__init__(timeout=None)
        self.address:str = "不明な宛先"
        self.content:str = "メッセージ未入力"
        self.game = game
        
    @discord.ui.button(label="メッセージを入力する")
    async def input_callback(self,interaction:discord.Interaction,button:Button):
        await interaction.response.send_modal(InputModal(self))
        
    #HACK: 送信先が選択されるまでdisableにしたい/selectのcallback関数からアクセスする方法がわからない
    @discord.ui.button(label="送信する")
    async def button_callback(self,interaction:discord.Interaction,button:Button):
        # 送信先が未選択 or メッセージ未記入 ならスルー
        if self.content == "メッセージ未入力":
            await interaction.response.send_message(embed=GetErrorEmbed('未入力の項目があります'))
            return
        self.game.Players['帝秀一'].waiting_embed = None
        
        # 送信できるか確認
        try: 
            self.game.Players['帝秀一'].SendMessage(self.address)
        except Exception:
            await interaction.response.edit_message(embed=GetErrorEmbed('既にメッセージを送信しています'))
            return
        # 実行
        for player in self.game.Players.values():
            if not player.role: continue
            if player.role.name==self.address: await player.ReceiveMessage(self.sender.role.name,self.content,self.is_reply)
        await interaction.response.edit_message(view=None,embed=discord.Embed(title=f'以下のメッセージを送信しました',description=f'帝秀一からメッセージが届きました\n\n{self.content}'))
        
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

# 回答入力フォーム
class AnswerInputForm(View):
    def __init__(self,game:Game,respondent:Player,questions:list,title:str):
        super().__init__(timeout=None)
        self.respondent:Player = respondent
        self.title = title #入力フォームembedのタイトル
        self.game = game
        self.questions = dict()
        for question in questions:
            self.questions[question] = '未入力'
        
    @discord.ui.button(label="回答を入力する")
    async def input_callback(self,interaction:discord.Interaction,button:Button):
        await interaction.response.send_modal(AnswerModal(self.questions.keys(),self))
        
    @discord.ui.button(label="送信する")
    async def button_callback(self,interaction:discord.Interaction,button:Button):
        self.respondent.waiting_embed = None
        await self.game.admin.send(embed=self.GenerateInputStatus())
        await interaction.response.edit_message(content='以下の内容で送信しました',view=None,embed=self.GenerateInputStatus())
        
    def GenerateInputStatus(self) -> discord.Embed:
        embed = discord.Embed(title=self.title)
        for key,value in self.questions.items():
            embed.add_field(name=key,value=value,inline=False)
        return embed

# 質問(上限5)に回答する汎用的なモーダル
class AnswerModal(discord.ui.Modal,title='回答せよ'):
    def __init__(self,questions:list,view:AnswerInputForm):
        super().__init__(timeout=None)
        self.view = view
        if len(questions)>5: raise Exception('Modalの上限は5です')
        for question in questions:
            self.add_item(discord.ui.TextInput(label=question))
            
    async def on_submit(self, interaction: Interaction):
        d = self.view.questions
        for i in range(len(self.children)):
            d[self.children[i].label] = self.children[i].value
        self.view.questions = d
        await interaction.response.edit_message(embed=self.view.GenerateInputStatus(),view=self.view)
# Jack Ability
class ViewForJack(View):
    def __init__(self,player:Player):
        super().__init__(timeout=None)
        self.player:Player = player
        
    @discord.ui.select(options=[discord.SelectOption(label=key) for key in text_data['Jack_Ability']],
                       placeholder='ここを押して選択')
    async def callback(self,interaction:Interaction,select:Select):
        select.disabled = True
        # チェック
        if self.player.role.remaining_ability_usage <= 0:
            await interaction.response.edit_message(embed=GetErrorEmbed('能力の使用可能回数が残っていません'))
        if self.player.role.is_ability_blocked:
            await interaction.response.edit_message(embed=GetErrorEmbed('能力の使用が妨害されています'))
        else:
            self.player.role.remaining_ability_usage -= 1
            await interaction.response.edit_message(view=self)
            await SendSystemMessage(self.player.channel,f'残り{self.player.role.remaining_ability_usage}回',
                                    headline=select.values[0],content=text_data['Jack_Ability'][select.values[0]])
        self.player.waiting_embed = None
    
# Jokerの役職選択
# 注意: StartGameからのみ呼ぶ(再帰するので)
class ViewForJoker(View):
    def __init__(self,game:Game):
        super().__init__(timeout=None)
        self.game = game
    @discord.ui.select(options=[discord.SelectOption(label=name) for name in ['エース','クラブの３','ジャック','クイーン','ランダム']])
    async def callback(self,interaction:Interaction,select:Select):
        select.disabled = True
        res = select.values[0]
        if res=='ランダム':
            res = ['エース','クラブの３','ジャック','クイーン'][random.randint(0,3)]
        self.game.Roles['ジョーカー'][0].role.escape_condition = role_data[res]['escape_condition'].split('もしくは')[0] #"もしくは"以降はコピーしない
        await interaction.response.send_message(content=f'{res}をコピーしました')
        await self.game.StartGame() #再帰
       
# Queen/Joker(copy Queen)の回答
class ViewForQueen(View):
    def __init__(self,game:Game,player:Player):
        super().__init__(timeout=None)
        self.game = game
        self.player = player
        self.questions = self.player.role.answer_status
        
    @discord.ui.button(label='回答(前半)')
    async def former_callback(self,interaction:Interaction,button:Button):
        await interaction.response.send_modal(AnswerModal(['ジャック','キング','エース'],self))
    @discord.ui.button(label='回答(後半)')
    async def latter_callback(self,interaction:Interaction,button:Button):
        await interaction.response.send_modal(AnswerModal(['スペードの２','クラブの３','ジョーカー'],self))
    @discord.ui.button(label="一時保存・送信")
    async def button_callback(self,interaction:discord.Interaction,button:Button):
        self.player.waiting_embed = None
        self.player.role.answer_status = self.questions
        await self.game.admin.send(embed=self.GenerateInputStatus())
        await interaction.response.edit_message(content='以下の内容で送信しました',view=None,embed=self.GenerateInputStatus())
     
    def GenerateInputStatus(self) -> discord.Embed:
        embed = discord.Embed(title=f'{self.player.role.name}({self.player.player_name})の回答')
        for key,value in self.player.role.answer_status.items():
            embed.add_field(name=key,value=value,inline=False)
        return embed

# optionsはadd_optionで渡す
class ViewForPassword(View):
    def __init__(self,game:Game):
        super().__init__(timeout=None)
        self.game = game
    @discord.ui.select(placeholder="ここを押して選択")
    async def callback(self,interaction:Interaction,select:Select):
        date = datetime.datetime.now()
        d = {"桜姫舞香":"0310",
             "花村光輝":"0810",
             "朝比奈ひな子":"0301",
             "二宮杏奈":"0503",
             "剣崎蘇芳":str(date.month).zfill(2)+str(date.day).zfill(2),
             "土岐いちか":"8888",
             "海山月正":"9999"}
        await SendSystemMessage(self.game.Players[select.values[0]].channel,headline='パスワードを獲得しました',content=d[select.values[0]])
        await interaction.response.edit_message(embed=discord.Embed(description=f'{select.values[0]}にパスワード{d[select.values[0]]}を送信しました'))
            
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
                check = game.IsChannelReady()
                if not check==True:
                    await SendError(message.channel,f'{", ".join(check)}が未設定です\n!setで設定した後に!startで再開してください')
                    game.phase = "ゲーム開始前"
                else: await SendSystemMessage(game.loby,headline="ゲームを再開します",mention='@here')
            if game.phase == "ゲーム終了":
                await SendSystemMessage(game.loby,headline="ゲームが既に終了しています",content="新規ゲームを始める場合は「!start」を入力してください")
        else:
            await SendSystemMessage(game.loby,headline="新規ゲームデータを作成しました")
            game.Save()
    return game

async def DeleteGameData(game:Game,message:discord.Message):
    msg = await SendSystemMessage(message.channel,'本当にゲームデータを削除する場合は「yes」を送信してください')
    try:
        res = await WaitForResponse(message.channel)
        if not (res=='yes' or res=='YES'): raise Exception()
    except:
        await msg.edit(embed=GetErrorEmbed('中断しました'))
    if message.guild in games:
        games.pop(message.guild)
    with open('save_data.json','r') as f:
        try:
            save_data = json.load(f)
        except:
            save_data = dict()
    with open('save_data.json','w') as f:
        if game.admin.guild.id in save_data:
            save_data.pop(game.admin.guild.id)
            json.dump(save_data,f,indent=4)
    await SendSystemMessage(message.channel,'削除しました')
            
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
            await game.TriggerTimedEvent()
            game.Save() #オートセーブ 1分おき

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