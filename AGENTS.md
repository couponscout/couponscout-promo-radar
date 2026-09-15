ILANG
TYPE:agents PROJECT:couponscout LANG:zh

::STATE{@PROJECT, name:CouponScout Promo Radar, kind:静态优惠聚合站, runtime:GitHub Actions + Cloudflare Pages, lang:纯 Python 标准库, cost:零}
::STATE{@DATA, source:厂商自己的公开页面, storage:data/offers.json, refresh:每 6 小时}

::MODULE{WHAT|title:这个仓库是什么}
  一个零服务器零密钥零成本的优惠站。定时任务读各品牌自己的公开页面 提取真实价格
  写成 data/offers.json 再由 build.py 渲染成静态站 site/ 提交回仓库并部署到 Cloudflare Pages
  运行时不做任何推理 不调用任何付费服务 站点规则写在 .ilang/site.ilang 代码真的读它

::MODULE{FILES|title:每个文件干什么 改之前先看这}
  scraper.py | 抓取 唯一写 data/offers.json 的地方
  build.py | 渲染 唯一写 site/ 的地方 同时生成 sitemap.xml robots.txt 和全部 JSON-LD
  ilang_config.py | 解析 .ilang/site.ilang 的唯一入口
  templates/ | HTML 与 CSS 模板 用 string.Template 占位符
  data/offers.json | 数据集 每次运行整体覆盖 不要手改
  .ilang/site.ilang | 配置唯一真源 厂商清单 字段 规则 阈值 都在这里
  .github/workflows/update.yml | cron 每 6 小时跑 scraper 加 build 加 commit

::MODULE{ALLOWED|title:允许做的动作}
  ::RULE{加厂商或改厂商⇒改 .ilang/site.ilang 的 PROVIDERS 块 不许在代码里另写一份清单}
  ::RULE{改抓取阈值 字段 导航 站点名⇒改 .ilang/site.ilang 的 SETTINGS 或 RENDER 或 FIELDS 块}
  ::RULE{改页面外观⇒改 templates/ 下的模板 不要改 build.py 里的字符串}
  ::RULE{改抓取逻辑⇒改 scraper.py 但保持纯标准库 不许引入 pip 依赖}
  ::RULE{改完必须本地跑通 python scraper.py 然后 python build.py 确认 site/ 生成无报错}
  ::RULE{改了 .ilang/site.ilang 里一家厂商 重跑一次 站上就该变 变不了说明配置没被真读 那是 bug}

::MODULE{DATA|title:数据从哪来 只允许这些入口}
  ::RULE{厂商自己的公开商品 feed 例如 /products.json 和 /collections/<sale>/products.json}
  ::RULE{厂商自己的公开 sitemap.xml 里的商品页 页面上的 schema.org Product 与 Offer}
  ::RULE{厂商自己的官方优惠页 用它来判断这个页面是不是厂商自己划定的促销页}
  ::RULE{每个来源都必须遵守 robots.txt 网络错误可以放行 但 robots.txt 里明确禁止的必须停}
  ::RULE{每条优惠必须带 source_url 指向被抓的那个公开页面 拿不出来源的不许进数据集}

::MODULE{DEAL_CLASS|title:两种条目 不许混为一谈}
  verified_discount | 页面给了划线价 折扣百分比是由 price 和 list_price 算出来的
  sale_page | 条目读自厂商自己划定的促销页 只报真实价格 不报折扣百分比
  ::RULE{deal_class=sale_page 的条目不许倒推一个折扣出来 也不许在页面上暗示我们算过折扣}
  ::RULE{deal_class 只有这两个合法值 不许造第三个}

::MODULE{BOUNDARY|title:绝对不许做的}
  ::BOUNDARY{never:编价格 编优惠 编折扣百分比 编到期日 编佣金比例|scope:permanent}
  ::BOUNDARY{never:抓登录后的内容 绕反爬 伪造 User-Agent 冒充浏览器|scope:permanent}
  ::BOUNDARY{never:品牌词竞价 cookie 注入 自买自推 或任何需要绕开联盟条款的玩法|scope:permanent}
  ::BOUNDARY{never:把零售商 媒体导购站 比价聚合站当成品牌加进 PROVIDERS|scope:permanent}
  ::BOUNDARY{never:引入付费 API 需要密钥的服务 或运行时推理 这条管线必须能零成本无限跑|scope:permanent}
  ::BOUNDARY{never:把 .ilang/site.ilang 只当注释贴上去 配置不被代码读等于没有配置|scope:permanent}

::MODULE{HONESTY|title:遇到抓不到的时候怎么办}
  ::RULE{抓不到 price⇒这条不写 price 字段 也不写进结构化数据 直接丢掉这一条}
  ::RULE{抓不到 valid_until⇒不写该字段 不许编一个到期日}
  ::RULE{算不出折扣⇒不写 discount_percent 字段 宁可少一个字段也不许造}
  ::RULE{厂商完全没有机器可读数据⇒厂商页只链官方优惠页 并写清为什么没有条目 不许凑数}
  ::RULE{页面因此变瘦是正常结果 变胖靠的是加厂商或加语言 不是靠编数据}

::MODULE{SCHEMA|title:结构化数据不许破}
  ::MUST{每页一条 canonical 指向自己}
  ::MUST{详情页嵌 schema.org Product 与 Offer 带 price priceCurrency availability url}
  ::MUST{厂商页有价格区间时用 AggregateOffer 写 lowPrice highPrice offerCount}
  ::MUST{列表页和对比页用 ItemList 每项带 position 与 url 有层级就加 BreadcrumbList 有问答就加 FAQPage}
  ::MUST{sitemap.xml 的 lastmod 用真实抓取时间 不许写死一个日期}
  ::MUST{多语言时必须加 hreflang 互指 加一条 x-default 指向主语言}

::MODULE{SCALE|title:要扩市场时怎么做}
  ::RULE{第一版只做一个语言 跑通了再复制}
  ::RULE{扩语言用同一个域名加语言子目录 /de/ /ja/ /es/ 不许开新域名 年份要集中在一个域名上}
  ::RULE{扩地区要抓那个地区的优惠页 用当地货币 文案用当地语言 不许把英文机翻一遍就当新站}

::FACT{key:protocol|value:I-Lang|conf=confirmed}
::LESSON{id:ilang_note|type:meta|scope=global|conf=confirmed}
  这份文件用 I-Lang 协议写 给以后接手这个仓库的 AI 看 想学这套写法去 ilang.ai
