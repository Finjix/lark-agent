<!--
GOF (无尽冬日)'s fixed review spec. For a review, this comment is removed and the rest is given to the model (Gemini) as it is.
Source: 《无尽冬日 广告素材审核标准（2026-10 版）》 https://centurygames.feishu.cn/docx/CSy2dZ3XvoLDmBxolPXcNbtunuc
  It merges: 无尽冬日-广告素材审核规则, 【红线】无尽冬日-星广素材审核规则, 字节素材拒审文档, 巨量引擎游戏行业素材审核规则（2026-01-16 版）.
Written up 2026-11-02 and translated into English 2026-11-17: the content follows the original, only listed item by item; the original's
Chinese terms, examples and wordings are kept verbatim (they are what the material is checked against), with English glosses where they
help. The last item of section 6 (the review does not check how large a number is) was added when writing it up, so that the model does
not report every older number as a problem. The original's example pictures are not included; the upload and submission process (NAS,
创量, 热云, CLS, the review drive) cannot be checked from the material and is left out.
When a new edition of the standard comes out, update this file to it. Section titles keep their Chinese names (红线, 字幕和口播, …): the
review prompt and the dynamic spec's categories refer to them.
-->

# GOF (无尽冬日) ad material review spec (2026-10 edition)

Where the sources disagree, this spec decides; whatever it does not cover, or you are unsure of, ask in the review group first.
A red-line breach means at best account throttling and the material taken down, at worst public outcry and lawsuits, and is followed up with whoever is responsible.

## 1. 红线 (red lines): touching one means redoing the whole video

### 侵权 (infringement)
- Not allowed: footage of internet celebrities (网红), film and TV stars, actors and entertainers used in a mash-up or as an eye-catching opening.
- Not allowed: internet memes about stars and influencers, e.g. 蔡徐坤、菜坤、明星打篮球的表情包.
- Not allowed: content published by Douyin accounts or accounts on other platforms.
- Not allowed: another brand clearly visible.
- Not allowed: trending Douyin BGM, songs by popular singers.
- Rejected before: ByteDance (字节) rejected 2 celebrity mash-ups because the portraits were not licensed; in an AI video a luxury car's grille made to look like a Rolls-Royce's was judged infringement.

### 肖像授权 (portrait licences)
- Real people's portraits without a signed licence may not be used; sketched portraits and memes of real people count too.
- A licence for a film or TV series is not a licence for the actors' own portraits.
- 巨量 (Ocean Engine) reviews this under Article 33 of the Advertising Law.

### 公司名誉 (company reputation)
- No company or entity names.
- Do not mock the boss, producers or game designers; star-promotion (星广) material does not touch them at all.
- No buying or selling of high-level accounts, internal servers, or shills (当托).
- Real player nicknames, IDs and alliance names must be blurred; the alliance chat screen is where blurring is most often missed.
- Do not make a story of big spenders (大R), do not mock players.

### 政治 (politics)
- No words or images of national leaders, revolution, corrupt officials, self-immolation and the like.
- No use of the name of state organs, national symbols, national policies.
- No 「专供」 or 「特供」 (exclusive supply).
- No Japanese elements of any kind (star promotion recently stressed this).
- No 64 or tanks, including 648, 64 元礼包, or a 64 within a combat-power number; always avoid them around sensitive dates, and avoid them as far as possible otherwise.

### 低俗 (vulgarity)
- No pornography, sexual innuendo, revealing clothes, men and women in bed, harmful messages.
- Do not objectify women, do not rank female players.
- From 2026-07 on, no borderline (擦边) content at all.
- Rejected before: a 3D character with 「卡通描边」 (cartoon outlines) was judged to be revealingly dressed.

### 烟酒赌 (tobacco, alcohol, gambling)
- No tobacco, smoking or drinking, not even in the avatars of a comment section or a chat window.
- No footage of slot machines, fishing-game machines (捕鱼机) or fruit machines.
- Card games and mahjong may not show chips or cash at the same time.
- No words like 盘口、上分、下分、荷官、赔率、返水 (betting terms).
- Rejected before: 「打孔捕鱼」 was judged medium risk.

### 暴力血腥 (violence and gore)
- No brutal killing, splattering liquid, dismemberment, corpses.
- No guns and ammunition, no controlled knives; no newly added guns, cannons or military hardware, no military imagery.
- Nothing gloomy, no ghosts or monsters, no funerals.
- Rejected before: a bear shot in the head with red liquid splashing and blood and chunks of flesh on the ground; soldiers shooting at a mammoth with corpses, fire and thick smoke in the picture.

## 2. 字幕和口播 (subtitles and voice-over)

This is where ByteDance rejected the most this year; many rejections came down to a single word in the subtitles. Check subtitles, on-screen text, voice-over and dialogue against this table.

| Problem | Rejected before / not allowed | What to do |
| --- | --- | --- |
| 谐音、拼音、字母代字 (homophones, pinyin, letters for characters) | 没水你活不下去啊（谐音没死）、读jia书、升级rongru、见G了、嗨B | Write proper Chinese characters; rephrase a word that cannot be said |
| 礼包码 (gift codes) | LNW666、LNW886、LNW888（字母代人名） | Chinese plus digits, e.g. 林子聪666、林子聪888 |
| 英文 (English) | SSR 英雄、BOSS、Buff | Write 传说英雄、怪; replace other English with Chinese too |
| 极限词 (superlatives) | 国家级、世界级、最、2025TOP 年度游戏 | Replace with T0、超、无敌、第一; the game's own 「最大生命值」「最低伤害」 are fine |
| 金额 (amounts of money) | 一分钱也不用充、这个礼包不得200多块钱; no amount with a unit; the character 「钱」 is a sensitive word too | Only the number without a unit, e.g. 128; blur the ¥ sign |
| 付费 (paying) | 零氪也能玩、平民也能建起大熔炉；资源自动采集、自动建造 (in the same video as the first two, judged by ByteDance to suggest loot boxes) | Only 「不用氪金」 may be said |
| 没法证明的数字 (numbers that cannot be proven) | 地球人都在玩、70%的人都活不过第二关, best-seller chart rankings, specific dates and data | Do not write them; player counts as in section 4 |
| 限时限地 (limited time or place) | 仅限某地、某个时间参与、截止; sentence patterns like 「某地的人都…」「多少岁的都…」 | Do not write them |
| 防沉迷 (anti-addiction) | 熬夜、通宵、摆烂、玩不完、一玩玩一天、停不下来、上瘾、上头 | Do not write them |
| 敏感词 (sensitive words) | words about crime and war, 高锰酸钾、卧槽、滚、绿茶婊、矮矬穷、摸鱼、白嫖、躺平、昏君、icu, anything about the government, military or politics; flagged again by reviewers this year: 赚米、故伎重施、力工、男人排解压抑的加油站 | Do not write them; nor swearing |
| 吓人、催人 (scaring, pressuring) | 「救命啊!!!」 with a red arrow pointing at ice | ByteDance judged it false urgency |
| 标题党 (clickbait) | clickbait titles that mislead users, deliberately sensational pseudo-science; fake alerts like 手机已中毒、恭喜你中奖了 | 巨量 counts it as false and misleading: do not make it |
| 新闻体 (news style) | imitating news broadcasts or official announcements | ByteDance rejected a large batch from March to June: do not make any more |

## 3. 画面和界面 (picture and UI)

### 玩法提示语 (gameplay disclaimer, required)
All material that shows gameplay must carry the disclaimer, quoted gameplay clips included:

> 广告创意仅供参考，广告素材宣传内容须达到一定关卡或触发相应模块后出现，实际以游戏内容为准

- Place it in the lower middle of the picture, not against the left or right edge, shown in full in both landscape and portrait.
- 「须」 written as 「需」 is fine too: the official warning PNG uses 「需」.

### Other
- Buttons that cannot be tapped must be blurred or masked: 立即领取、立即下载、关闭、跳过、返回、复活、滑动解锁, video play buttons, 点击试玩.
- No arrows, no finger icons pointing at something to tap; the voice-over and gestures must not urge users to tap either.
- Do not imitate system screens or other apps: 版本更新、青少年模式、微信到账、红包、来电、直播间.
- China-server footage only: footage showing 「寒霜启示录」 is the international server and may not be used.
- No footage or elements of other games.
- Careless mistakes: a mirrored LOGO, blurry footage, blank or black screens, pre-registration content, app and mini-program material mixed up, the cover cut into the video.
- Subtitles inside the CapCut (剪映) safe area, the font neither too large nor too small; name tags do not cover faces.
- The opening does not flash quickly or dazzle, and does not start with a disgusting image, e.g. cutting open an overripe dragon fruit whose dense seeds trigger trypophobia.

## 4. 和游戏对得上 (matching the game)

Combat power, levels, rewards and heroes follow the game; exaggerating them draws player complaints, e.g. 「0氪三天战力一个亿」.

### 熔炉等级和战力 (furnace level and combat power)
These are the numbers an ad may state; do not estimate days or levels outside the table. Unless the player is said to be a paying player, do not state combat power over 100 million (亿).

| Player | Days | Furnace level | Combat power |
| --- | --- | --- | --- |
| Not paying, by days since the server opened | 3 天 | 15 级 | 300 万 |
| Not paying, by days since the server opened | 7 天 | 18 级 | 600 万 |
| Not paying, by days since the server opened | 10 天 | 20 级 | 800 万 |
| Not paying, by days since the server opened | 15 天 | 22 级 | 1000 万 |
| Not paying, by days since the server opened | 30 天 | 25 级 | 2000 万 |
| Not paying, by days since the server opened | 45 天 | 28 级 | 3000 万 |
| Not paying, by days since the server opened | 57 天 | 30 级 | 5000 万 |
| Paying, by days since registration | 3 天 | 30 级 | 6000 万 |
| Paying, by days since registration | 7 天 | 30 级 | 1 亿 |
| Paying, by days since registration | 10 天 | 30 级 | 1.3 亿 |

When only the furnace level is given, without days, use this table (accounts registered within 180 days):

| Furnace level | Combat power |
| --- | --- |
| 15 级 | 250 万 |
| 20 级 | 600 万 |
| 25 级 | 1800 万 |
| 30 级 | 5000 万 |

- 「熔炉 20 级」 with 「战力 2000 万」 has been sent back.
- The old star-promotion table's 「0 氪 3 天 200 万、7 天 400 万」 is void: use the two tables above.

### 福利 (rewards)
Only these four wordings, nothing more:
- 第二天签到就送传说英雄茉莉 (not 「SSR 英雄茉莉」)
- 首充就送娜塔莉亚
- 登录就送史诗英雄巴希提
- 首次抽卡必出史诗英雄谢尔盖

Hero levels, hero quality, and heroes not yet released on the China server must be checked against the game (《无尽冬日福利设定》《无尽冬日-英雄系统汇总》); one that appears in the material and is not covered above is 需确认.

### 玩家人数 (player numbers)
- 全球玩家 3 亿 (300 million players worldwide); do not say 1 亿.
- ByteDance rejected several 「三亿人都在玩」 for lack of proof: the proof submitted only covers 100 million registrations.

### 玩法和世界观 (gameplay and world setting)
- AI world-setting material shows no dungeons the game does not have, and the setting and levels match the game: e.g. 火晶 unlocks at level 30, so writing level 25 is wrong.
- When the copy says 「广告里啥样，游戏就啥样」 (the game is just like the ad), the B part must be something the game really has or that makes sense: conveyor belts for efficiency are fine; oil, gold coins and the like, which have no counterpart in the game, are not.

## 5. 按素材类型 (by material type)

### 3D 玩法 (3D gameplay)
- Only this project's assets; do not use other projects' creative assets directly. Assets on the MCC platform must be made for this project; new 2D assets are made separately.
- Keep the ice and snow elements, do not leave the ice-and-snow theme; after resizing, check that the icy atmosphere has not been cropped away.
- Fighting bears and monsters shows no red: no splattering blood, blood stains, chunks of flesh or corpses.
- No newly added guns, cannons or military hardware, no military imagery.
- Do not make it look like a fishing-game machine: turrets shooting at shoals of fish, multipliers on the fish, coins bursting out on a hit; such footage is judged strictly as gambling.
- Characters are not revealingly dressed; 3D cartoon-outlined characters have been judged so.

### 解说和迭代 (commentary and iterations)
- Quoted gameplay clips carry the disclaimer too.
- Comment-section and chat-window screenshots: avatars use neither real people nor cartoon figures, no tobacco, nicknames blurred, no finger icon next to them.
- Before making a mash-up, check it against 侵权 in section 1.

### 真人实拍 (live action)
- From 2026-07 on, no borderline content at all: men and women in bed, revealing clothes and suggestive lines all count.
- Do not use the vulgar actors competitors often use.
- Staff may only be shown as players misunderstanding them and then finding the mistake was their own; no malicious stereotypes.
- No pitting men against women, no ranking female players.
- No riding on unhealthy trending topics.
- No swearing.
- BGM uses no trending Douyin songs or songs by popular singers: they have commercial licensing problems.

巨量 also lists two kinds of plot it rejects:

| Kind | Plots |
| --- | --- |
| 虚假情景演绎 (staged fake scenarios) | 拿弱势群体做悲情营销、下跪送锦旗、违反常理的赌誓、拿证件担保、冒充权威身份、过去和现在的生活对比、砸钱撒钱撕钱、维权调解、仿官方通告、打假投诉卸载、夸大灾害、考验人性 |
| 违背主流价值观 (against mainstream values) | 上班摸鱼、「性别 + 任你选」、性别歧视、伪娘和男扮女装、未婚先孕、啃老、大胃王、摆烂躺平、不孝、违法行为、拜金炫富 |

### AI 类 (AI material)
- The world setting and levels match the game: see section 4.
- Car badges and grilles that look like a real brand are judged infringement: a grille like a Rolls-Royce's has been sent back.
- A cartoon figure's face must not look like an existing IP: a cartoon bull's face passed only once it no longer looked like 「牛来」.

### 明星素材 (celebrity material)
Titles exactly as the contracts say:

| Celebrity | Title |
| --- | --- |
| Tim | 品牌推荐官 |
| 范明、姜超、刘奕君 | 明星推荐官、明星玩家 |
| 小沈阳、李乃文 | 代言人 |

- Names are not abbreviated to letters: 「LNW666」 and the like have been rejected by ByteDance.
- ByteDance rejected 2 celebrity mash-ups because the portraits were not licensed; for material with a celebrity, confirm with 投放 (the media buyers) before it goes out that the licence documents are in the ad account.

## 6. 命名 (naming)

Format: GOFCN编号_产出方_一级标签_迭代标签_自定义名 (GOFCN number_producer_primary tag_iteration tag_custom name), e.g. GOFCN3394_DR_汉化_迭代_真人吃瓜

| Field | Rule |
| --- | --- |
| 编号 (number) | Unified numbers start at 100000; the 90000 range has been given to agency buyers (代投) |
| 产出方 (producer) | DR individual freelancer, ZC in-house, WA outsourcing vendor; an agency buyer writes the initials of its own name, e.g. 微电 writes WD |
| 一级标签 (primary tag) | 汉化、重度解说、轻度解说、真人剧情、真人解说、AI、KOL |
| 迭代标签 (iteration tag) | May be left out when it is not an iteration |

- The material type and setting version in the file name match the content.
- Landscape and portrait versions share one number and are changed and delivered together.
- Numbers are assigned (3394 in the example above is an older number): the review does not check how large a number is, only the format, the producer, the primary tag, and whether the file name matches the content.

## 7. 成片质量 (quality of the finished video: check it yourself before delivery)

- Listen to the whole sound track from start to end.
- Lip sync matches.
- Parts A and B have the same frame format, and the start of part B follows on from the end of part A.
- A commentary's part B is cut at the start of a full sentence, not mid-sentence.
- Long commentary has subtitles.
- No single wrong frames flashing.

## 8. 交审前自查 (self-check before submitting)

- The disclaimer is there, in the lower middle, complete in landscape and portrait
- No arrows, finger icons, or buttons that cannot be tapped
- No homophones, pinyin, letters for characters, or English in subtitles and voice-over
- No superlatives, amounts with units, or ¥ signs
- Paying is described only as 「不用氪金」
- Combat power, levels, rewards and heroes all match section 4; the player number given is 全球 3 亿
- China-server footage, no 「寒霜启示录」, no other games
- No company names, bosses or designers, real player nicknames or alliance names
- No footage of stars or influencers, trending memes, trending BGM, or other brands
- No blood, corpses, newly added guns or cannons; no Japanese elements and no 64
- No borderline content, no vulgar actors used by competitors
- Naming correct, landscape and portrait delivered together
