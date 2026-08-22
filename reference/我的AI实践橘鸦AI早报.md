![cover_image](https://mmbiz.qpic.cn/sz_mmbiz_jpg/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibc2X4LDtfJzTPtCTHBJYqxagVsIeBujns6V9srydKLuv3aVfazKGxHpw/0?wx_fmt=jpeg)

#  我的 AI 实践：橘鸦 AI 早报

原创  Juya  Juya  [ 橘鸦Juya ](javascript:void\(0\);)

_2025年12月10日 09:00_ __ _ _ _ _ _ 广东  _

在小说阅读器读本章

去阅读

![](https://mmbiz.qpic.cn/sz_mmbiz_jpg/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibcdtD0mGNvQFo1RQfBfYibRfbY2ZFafDsdiaibbbMSt4oiah3F4UicjuE83Gw/640?wx_fmt=jpeg&from=appmsg)

#  橘鸦 AI 早报的制作

鉴于很多读者对早报的制作流程感兴趣，今天写文分享整个过程的细节。因为整个项目全都是 AI 浇筑的💩山，所以暂时不会开源。此外，本文的视频版已发布在哔哩哔哩
` BV1B82DBdEdP  ` ，感兴趣的读者可以前往观看。

总的来说，早报的制作主要由  ** 信息采集  ** 、  ** 筛选处理  ** 、  ** 修饰分发  **
三个环节组成。在速度、成本和能力的限制下，目前早报的制作尚未涉及  ** Agent  ** ，整体上是一个掺杂了人工处理环节的简单 Workflow。

* * *

##  一、 信息采集

###  1\. RSS 优于爬虫

![](https://mmbiz.qpic.cn/sz_mmbiz_jpg/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibcjCC6eiaxWswrPm6l8MkZnNPo0jribFo1VNbHAcJrgMzhrenbicbSbCavw/640?wx_fmt=jpeg&from=appmsg)

在信息采集环节，我主要使用的是  ** RSS  ** 收集资讯。相比于爬虫，RSS 具有以下显著优势：

  1. 很多网站本身原生支持 RSS，例如  ** LINUX DO  ** 社区和  ** Reddit  ** 。 
  2. 对于不支持 RSS 的主流网站，往往存在相对成熟的解决方案（如  ** RSSHub  ** 等）。 
  3. RSS 通过  ** XML  ** 文件交换信息，数据已做结构化处理，且保留了正文的一定样式。 

![](https://mmbiz.qpic.cn/sz_mmbiz_png/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibcGIlpUG0z4ZibdLaHZ5KUxbf8jiacoib6Kx6Casp2gglYFRXA9kK638c8A/640?wx_fmt=png&from=appmsg)

当然，手动采集信息的环节也是必不可少的，我安装了浏览器插件  ** Obsidian Web Clipper  **
，用于手动提取页面元素，转化为带元信息的  ** Markdown  ** 文本，再通过 AI 编写的插件发送至后台。

![](https://mmbiz.qpic.cn/sz_mmbiz_jpg/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibcic0IHfjVCkZVxyKCh78tlicqePvF9ZibvZsSbX2K0m4oVvOhvI31S4lGA/640?wx_fmt=jpeg&from=appmsg)

###  2\. 核心信息来源

更多的人可能好奇的是，为什么早报每天有如此丰富的资讯，这应该要归功于信息源的广度。

![](https://mmbiz.qpic.cn/sz_mmbiz_jpg/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibcDWChqqMfBHicUqDWI7h23LRRJ4FRzicCJ6yzTibiaClFvZLa6BRSsyPibhA/640?wx_fmt=jpeg&from=appmsg)

除了前面提及的  ** LINUX DO  ** （AI社区）和  ** Reddit  ** （LocalLlaMA板块，讨论开放权重AI），  ** X
** （社交媒体网站）更是全世界 AI 信息的前沿。

你能想到的几乎国内外所有 AI 公司或者大公司的 AI 相关部门都在上面设置了账号发布信息：

类别  |  账号/品牌 (部分)  
---|---  
** 国际厂商  ** |  ** OpenAI  ** 、  ** Google DeepMind  ** 、  ** Anthropic  ** 、
** xAI  ** 、  ** Meta  **  
** 国内大厂  ** |  ** 阿里Qwen  ** 、  ** 腾讯 Hunyuan  ** 、  ** 字节跳动  ** (  `
ByteDanceOSS  ` )、  ** 美团 LongCat  ** 、  ** 快手 KwaiKAT  ** 、  ** 小米 MiMo  ** 、
** 小红书  ** (  ` Rednote hi lab  ` )等等。  
** 创新/独角兽  ** |  ** DeepSeek  ** 、  ** 智谱 Z.ai  ** 、  ** Kimi  ** 、  **
MiniMax  ** 、  ** 阶跃 StepFun  **  
  
此外，节目也吸引了一批紧跟  ** AI  ** 热点的读者和观众，评论区、私信和交流群也是我获取最新资讯的重要渠道。

###  3\. 跨行业迁移的局限性

可能有读者想制作一个其他类型的“早报”，但我认为  ** AI  ** 资讯的优势在于其主要通过文字传达，而且大部分  ** AI 公司  **
会积极在社交媒体发布相关信息，这意味着如果迁移到其他在互联网上缺乏足够文本信息（搜索引擎检索不到相关信息）的行业，需要额外的人力做信息采集，否则自动化效果会大打折扣。

![](https://mmbiz.qpic.cn/sz_mmbiz_jpg/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibcE8tibR3Pr4MKB4bDO9A5A01GHRk5ZXG3MSolFz70LJgQwVhMaQMVbeQ/640?wx_fmt=jpeg&from=appmsg)

即便在  ** AI  **
领域，国内企业往往倾向于使用短视频、精美图片卡片宣传，或者仅投放自媒体而缺乏官方正式文本，甚至有的只开线下发布会线上仅有发布会简报。这也给我报道国产  **
AI  ** 产品带来非常大的困扰。

* * *

##  二、 信息筛选和处理

###  1\. 自动化初筛与处理

![](https://mmbiz.qpic.cn/sz_mmbiz_jpg/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibcLQ1htf5jT6lxynTnaULfIl9fmzw1NFV4PRFeNcfrAO4zNDibZyiaKT5w/640?wx_fmt=jpeg&from=appmsg)

采集到信息后，首先由  ** AI  ** 进行初筛，剔除“与 AI 无关”或“无报道价值”（如单纯的使用体验或教程）的内容。随后，系统并行执行以下 4
项任务：

任务名称  |  任务描述  
---|---  
** 摘要生成  ** |  ** AI  ** 为每条信息制作  ** 50  ** 字摘要，辅助人工快速判断。  
** 关键词提取  ** |  提取如  ` Gemini 3  ` 等关键词，将资讯归类到具体事件。这一步会把已有的关键词也加入上下文供 AI 挑选。  
** 旧闻排除  ** |  Stage A 只保留日报日期前一天 00:00（Asia/Shanghai）之后的资讯，再与上一次早报内容进行比对，排除重复信息。
** 智能打分  ** |  ** AI  ** 对信息价值打分，低于阈值直接排除，分值供人工筛选参考。  
  
在 AI 做完以上工作后，可以得到分类好的一系列信息，并且每条信息都附上了概要和打分。

###  2\. 深度整合与人工介入

![](https://mmbiz.qpic.cn/sz_mmbiz_jpg/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibcm8mGpXuBwoR9bgWfjr4cmyia70YYlhJibcpadicoel8gSJzMPaOo2ZZyQ/640?wx_fmt=jpeg&from=appmsg)

早报不会直接将所有通过 AI 筛选的信息丢给 AI，而是通过人工只保留最官方、最准确或最早的信源。大部分时候，还会人工补充更多的官方信息。

随后，就是正式生成早报内容的环节：

  * ** 内容生成  ** ：每个分类下的信息合并后作为上下文，由  ** AI  ** 生成标题、正文。 
  * ** 人工调整  ** ：对生成内容进行校对，保留来源链接，并挑选合适的视频或图片素材。 

![](https://mmbiz.qpic.cn/sz_mmbiz_jpg/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibc52s4NDQ9r43OTt3SDXzpoWaqUPCrq2IEKhzaLnBDQSwpicnCQNNic9tQ/640?wx_fmt=jpeg&from=appmsg)

最终我们就得到了很多条资讯，每条资讯都配备好标题、正文、链接和媒体资源。

![](https://mmbiz.qpic.cn/sz_mmbiz_jpg/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibcXNUlgyeQgbVnws2A0kqY11IpxUKg8JtI6EuCrD1X4wqhrzBNejib99Q/640?wx_fmt=jpeg&from=appmsg)

###  3\. 全自动 vs 人工

虽然项目技术上可以实现全自动处理（市面上已有不少此类 AI 资讯汇总），但我选择加入“亿点点”人工。

这种人工投入主要用于核查事件的真实性与准确性、努力做到最全最新并修补  ** AI  ** 产生的  ** 幻觉  **
。但在有限的成本和时间以及繁多的事件等各种因素限制下，早报依旧有很多不足，每期有什么错漏还请各位读者在评论区指出。

###  4\. 推荐模型和平台

整个流程中涉及到非常多要处理的信息和每条信息有非常多 AI 处理环节。

目前我主要接入的是  ** GMI Cloud  ** 推理引擎平台上提供的模型，在生成环节使用  ` Kimi K2 Thinking  `
，其他任务主要使用  ` Qwen3 Next 80B A3B Instruct  ` ，整体上很好地权衡了效果、成本和速度。

![](https://mmbiz.qpic.cn/sz_mmbiz_png/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibc3jZDINZvreTtojIyJVghxicnZ9C995t9olZILW09qX5yCNlTLIgcZgA/640?wx_fmt=png&from=appmsg)

** GMI Cloud  ** 是全球仅有的六家  ** Reference Platform NVIDIA Cloud Partners  **
之一，上面的模型推理速度都很快，感兴趣的读者可以前往  ` https://console.gmicloud.ai  ` 体验一下，使用  `
IETRIAL  ` 可以兑换  ** 5美元  ** 赠费。

![](https://mmbiz.qpic.cn/sz_mmbiz_jpg/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibcUg3LicYtjNc07bVaBJ6gDLBJNfb1W1RjialOXA5EV9gbxgdA4U4hmVjw/640?wx_fmt=jpeg&from=appmsg)

* * *

##  三、 信息的修饰与分发

以防止有人还不知道，在这里我要补充一下，目前早报提供  ** 文字版  ** 和  ** 视频版  ** ，并在  ** 公众号  ** 、  **
哔哩哔哩  ** 、  ** 知乎  ** 、  ** YouTube  ** 、  ** 小红书  ** 和  ** 抖音  ** 同步更新。

###  1\. 各平台特性对比

平台  |  特点与优势  
---|---  
** 公众号  ** /  ** B 站  ** |  ** 优先发布渠道  ** ，目前唯二为我提供收益的平台。  
** 知乎专栏  ** |  支持  ** 目录  ** 功能，可以便捷切换事件，阅读体验较好。  
** YouTube  ** |  ** 从未限制过我的内容  ** （包括简介和字幕），会上传外挂字幕，系统会自动切分片段，观看体验最佳。  
** 小红书/抖音  ** |  限制最严格。  
![](https://mmbiz.qpic.cn/sz_mmbiz_png/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibcktedd7ezxAHygjdO7LiaXKiaGofB0YuSsF6swDVC5qWswjibd355Qa7og/640?wx_fmt=png&from=appmsg)

###  2\. 文字版制作

对于文字版，我会将组合起来的正文发由  ** AI  ** 进行  ** Markdown  ** 格式优化排版。文字版早报的每一个  ** 加粗  **
、  ` 行内代码块  ` 和表格其实都是由 AI 决定并修改的。

同时主标题、目录和每条资讯开头的摘要等元素也是在这个环节生成并组合。文字版在分发之前以 Markdown 文件形式存在。

![](https://mmbiz.qpic.cn/sz_mmbiz_jpg/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibc1U7fic9PDWyeIEZdMKd9NZfBksAefia7OFNwqsRIePkIrEszfPyqBkRw/640?wx_fmt=jpeg&from=appmsg)

知乎支持  ** Markdown  ** ，但是 Markdown 兼容有问题，有的时候会显示不出相关链接。

而公众号文章的格式是一种受限的  ** HTML  ** 格式（例如  ** 不能实现点击跳转  ** ）。但  ** Markdown  **
转公众号格式  ** HTML  ** 文本可以靠第三方工具实现（市面上非常多），有的工具（例如  ` doocs/md  ` ）还可以自定义 CSS
样式，能把 Markdown 文章按设定好的样式呈现。

![](https://mmbiz.qpic.cn/sz_mmbiz_png/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibcE0CUARuErv4lU40TVkb9KJ3iaKzblKibyIAU1IsicSXYA36iczAG4UeGhg/640?wx_fmt=png&from=appmsg)

这种样式转换也可以通过代码实现，我发现过一个叫  ` MurphyLo/md2juya  ` 的项目，可以将 Markdown
转化为橘鸦AI早报的样式。当然，这种样式的源头是  ** Claude  ** 。

![](https://mmbiz.qpic.cn/sz_mmbiz_png/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibcbQxF4XUUtV1OLsxfT29aR87UEquKhm94VjDomdfqubiatyE8vuaxGnA/640?wx_fmt=png&from=appmsg)
![](https://mmbiz.qpic.cn/sz_mmbiz_png/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibc9oob9hOdtBqCLQ7koOqyAENoQG9Nu0maSPViaEt2XdAkViboKiaJfGRHw/640?wx_fmt=png&from=appmsg)

###  3\. 视频版制作

视频版会复杂一些，但也没有很复杂。视频的基本元素是画面和声音。

** TTS  ** （文本转语音）是  ** LLM  **
时代之前就已经很成熟的技术。插句题外话，所有含科幻未来元素的文艺作品不该再用含机械合成风格的语音来呈现机器人/智能系统了。现阶段顶尖的 TTS
模型的效果已经足够让很多人意识不到这是合成语音。

在早报刚开始发布的阶段，自动生成包含大量文字的画面还没有什么特殊的办法，生成 SVG 或者 HTML 页面的能力彼时还只有  ** Sonnet  **
系列做得比较好。直到  ** DeepSeek V3 0324  ** 发布，我才负担得起成本渐渐开始使用生成 HTML 截图作为画面的方式。

![](https://mmbiz.qpic.cn/sz_mmbiz_jpg/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibcSsiaEmABGTD5AMMyBWwAagss1hD3s25ibr9XmwAB0fnPaGl6iajMsrK9Q/640?wx_fmt=jpeg&from=appmsg)

####  3.1 画面

目前早报的画面样式是模仿了  ** NotebookLM  ** 视频概览功能产物的卡片样式，Prompt 的核心规则有两条：

  1. 渲染在  ` 1920x1080px  ` 画布上（尺寸不重要，更重要的是保持 16:9 比例）； 
  2. 内容由卡片组成，卡片按一定规则进行排版。 

![](https://mmbiz.qpic.cn/sz_mmbiz_png/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibcZotT0OKy57IceJLJ3CIiaNCbGx1ic6Ey0EwuSXBS1SKrqenDBmEjzebw/640?wx_fmt=png&from=appmsg)

生成的 HTML 网页可以使用  ** Selenium  ** 通过  ** Chrome DevTools Protocol  ** 指令截取截图。

![](https://mmbiz.qpic.cn/sz_mmbiz_png/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibceOxPupOJsa1sU26a7AuTYFktxSF4LQ9qQUAd0efbA6KrsgXcH6Y4dw/640?wx_fmt=png&from=appmsg)

随着生图模型愈发强大，在未来如果成本合适，可能也会切换为使用生图模型直接生成画面。

####  3.2 语音

早报视频更核心的部分是语音。AI 根据正文生成口播稿，口播稿本身按新闻事件分段，在合成语音时，会额外再  ** 按标点符号拆分句子，每个短句作为一个请求
** ，通过  ** 计算每一个合成音频的时长  ** ，就可以确定下来整个视频的时间轴。

![](https://mmbiz.qpic.cn/sz_mmbiz_jpg/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibcSsiaEmABGTD5AMMyBWwAagss1hD3s25ibr9XmwAB0fnPaGl6iajMsrK9Q/640?wx_fmt=jpeg&from=appmsg)

有了时间轴，其他所有内容都可以一一确定：

  * 根据口播稿文本制作 SRT 字幕； 
  * 每个事件区间显示对应的画面； 
  * 在事件中间插入事件相关的图片或视频； 
  * 在事件切换时播放过渡效果声音； 
  * 制作视频进度条…… 

![](https://mmbiz.qpic.cn/sz_mmbiz_png/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibcHv0yfb7LKcn5R7icXcCrxgfJXygib6iad4M6ia2G8nKntShulgbicKMOb5w/640?wx_fmt=png&from=appmsg)

最终通过  ** ffmpeg  ** 就可以逐步合成最终版。

此前大部分时候，早报 TTS 使用的是  ** MiniMax  ** 的  ** Speech-02-HD  ** 模型 API 中的  `
Podcast_girl  ` 音色，不久前受限于成本，切换成了  ** Speech-2.6-Turbo  ** 模型。

* * *

##  四、 其他

###  取代橘鸦最简单的方法

很多 AI 应用 （  ** Grok  ** 、  ** 智谱清言  ** 等）都有定时任务功能，可以设定一个获取 24 小时内的 AI
新闻的定时任务，AI 会在指定的时间点将信息投递到你的邮箱。再使用  ** NotebookLM  ** 的视频概览功能，根据 AI
提供的资讯就可以制作视频了。

![](https://mmbiz.qpic.cn/sz_mmbiz_png/ykj6qYPSm3cO5znzwl3iclRRFhCyfXeibcIeDPlD3sLbt32WuoQcyhrrxvl52T5AwJuyibbVGEJkToNQ8co4gUYZA/640?wx_fmt=png&from=appmsg)

###  未来计划

2025 年还有一个月不到，我  ** 希望能  ** 把想做但一直没做的事情完成：

  * ** AI 资讯周报、月报系列  ** ； 
  * ** RSS 分发文字版  ** ； 
  * ** 早报原文上传到 GitHub  ** ； 
  * ** 新增竖屏视频版  ** ； 
  * ** 公众号新增正文更简洁的版本  ** ； 
  * ** 重新设计封面  ** 。 

敬请期待！

预览时标签不可点

[ 阅读原文 ](javascript:;)

微信扫一扫  
关注该公众号



微信扫一扫  
使用小程序

****



****



****



×  分析

__

![作者头像](http://mmbiz.qpic.cn/sz_mmbiz_png/ykj6qYPSm3eQcysMk2DhH4BSeLDfyUfuKaSf5M8siaSPNbepXtqTU4BK5WxlLkb8L8o6j6FupHbKxpG6z2iaAiaLQ/0?wx_fmt=png)

微信扫一扫可打开此内容，  
使用完整服务

：  ，  ，  ，  ，  ，  ，  ，  ，  ，  ，  ，  ，  。  视频  小程序  赞  ，轻点两下取消赞  在看  ，轻点两下取消在看
分享  留言  收藏  听过
