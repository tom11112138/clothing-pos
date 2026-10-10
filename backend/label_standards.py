"""Officially checked choices, not an automatic assertion that a garment complies."""
import re

CHECKED_ON = "2026-10-11"
PRODUCT_CATEGORIES = [
    {"name": "羊绒衫", "note": "先核对山羊绒成份和针织工艺；含羊绒不等于可以直接采用山羊绒针织品标准。"},
    {"name": "牛仔裤", "note": "区分机织牛仔布与针织牛仔面料，不能仅凭外观或弹性判断。"},
    {"name": "风衣", "note": "风衣标准针对机织面料风衣；特殊防护、防水等产品另核对厂家标准。"},
    {"name": "连衣裙", "note": "区分机织连衣裙与针织裙；原品类“莲衣裙”统一写为“连衣裙”。"},
    {"name": "羽绒服", "note": "区分机织与针织羽绒服；保留原标签上的羽绒种类、绒子含量、各规格充绒量等信息，不能只用这张贴纸替代完整标识。"},
    {"name": "羊绒大衣", "note": "机织毛呢大衣与针织大衣采用的标准不同；羊绒面料标准不能代替成衣执行标准。"},
    {"name": "休闲裤", "note": "普通机织休闲裤和针织休闲裤分开核对；西裤、紧身裤等按实际款式及厂家标准确认。"},
    {"name": "衬衫", "note": "区分机织衬衫与针织衬衫；长袖、短袖不是执行标准的唯一依据。"},
    {"name": "半袖", "note": "半袖是袖长俗称：针织T恤参考T恤衫标准，短袖衬衫参考对应衬衫标准。"},
    {"name": "棉服", "note": "棉服是带填充物的服装，不等于所有含棉服装；羽绒填充产品应另核对羽绒服装标准。"},
    {"name": "套装", "note": "没有适用于所有套装的统一产品标准。裙套、西服套装、针织休闲套装分开核对；各件标准、成份或安全类别不同的，须分别标识。"},
    {"name": "针织衫", "note": "针织只是工艺分类，还需区分山羊绒、羊毛、低含毛及仿毛等材料和实际款式。"},
]
EXECUTION_STANDARDS = [
    {"code": "FZ/T 73009-2021", "name": "山羊绒针织品", "categories": ["羊绒衫", "针织衫"],
     "scope": "山羊绒针织成衣；核对纤维含量和厂家采用标准，不用于直接判定机织羊绒大衣",
     "source": "https://std.samr.gov.cn/hb/search/stdHBDetailed?id=C3D579650B85AF60E05397BE0A0AB567"},
    {"code": "FZ/T 81006-2017", "name": "牛仔服装",
     "categories": ["牛仔裤"], "excludes_infant": True,
     "scope": "以纯棉或棉为主要原料的机织牛仔服装；不适用于36个月及以下婴幼儿服装",
     "source": "https://std.samr.gov.cn/hb/search/stdHBDetailed?id=8B1827F2456ABB19E05397BE0A0AB44A"},
    {"code": "FZ/T 73032-2017", "name": "针织牛仔服装", "categories": ["牛仔裤"],
     "scope": "针织牛仔服装；与机织牛仔服装标准区分，按厂家面料和供货资料核对",
     "source": "https://std.samr.gov.cn/hb/search/stdHBDetailed?id=8B1827F24256BB19E05397BE0A0AB44A"},
    {"code": "FZ/T 81010-2018", "name": "风衣", "categories": ["风衣"], "excludes_infant": True,
     "scope": "以纺织机织物为主要面料的风衣；不适用于36个月及以下婴幼儿服装",
     "source": "https://std.samr.gov.cn/hb/search/stdHBDetailed?id=8B1827F264E7BB19E05397BE0A0AB44A"},
    {"code": "FZ/T 81004-2022", "name": "连衣裙、裙套", "categories": ["连衣裙", "套装"],
     "scope": "机织面料连衣裙、裙套的参考标准；针织裙须另核对，不是所有套装的通用标准",
     "source": "https://std.samr.gov.cn/hb/search/stdHBDetailed?id=F9A74BF1D2BAEC70E05397BE0A0A8DAE"},
    {"code": "FZ/T 73026-2014", "name": "针织裙、裙套", "categories": ["连衣裙", "套装"],
     "scope": "针织裙、裙套；核对面料和厂家采用标准",
     "note": "2025版于2028-01-01实施，目前目录保留现行2014版。",
     "source": "https://std.samr.gov.cn/hb/search/stdHBDetailed?id=8B1827F22EEFBB19E05397BE0A0AB44A"},
    {"code": "GB/T 14272-2021", "name": "羽绒服装", "categories": ["羽绒服"],
     "scope": "机织羽绒服装的参考标准；核对羽绒种类、绒子含量及各规格充绒量，针织羽绒服另核对",
     "source": "https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=4C8640CD45AB3F46C5D9A990D4E8DC2F"},
    {"code": "FZ/T 73053-2015", "name": "针织羽绒服装", "categories": ["羽绒服"], "excludes_infant": True,
     "scope": "针织面料为主、羽绒为主要填充物的服装；不适用于36个月及以下或身高100cm及以下的婴幼儿服装",
     "note": "2025版于2027-05-01实施，目前目录保留现行2015版。",
     "source": "https://std.samr.gov.cn/hb/search/stdHBDetailedCNF?id=8B1827F2217EBB19E05397BE0A0AB44A"},
    {"code": "GB/T 2665-2017", "name": "女西服、大衣", "categories": ["羊绒大衣", "套装"],
     "scope": "女式机织毛呢类西服、大衣的参考标准；核对面料和成衣类型，不是羊绒面料本身的标准",
     "note": "2026版于2027-08-01实施，目前目录保留现行2017版。",
     "source": "https://std.samr.gov.cn/gb/search/gbDetailed?id=71F772D8234AD3A7E05397BE0A0AB82A"},
    {"code": "GB/T 2664-2017", "name": "男西服、大衣", "categories": ["羊绒大衣", "套装"], "excludes_infant": True,
     "scope": "男式机织毛呢类西服、大衣；不适用于36个月及以下婴幼儿服装",
     "note": "2026版于2027-08-01实施，目前目录保留现行2017版。",
     "source": "https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=346466004A5CD694BACB0721FB80733E"},
    {"code": "FZ/T 73058-2017", "name": "针织大衣", "categories": ["羊绒大衣"], "excludes_infant": True,
     "scope": "以针织物为主要面料的大衣；不适用于36个月及以下婴幼儿服装",
     "source": "https://std.samr.gov.cn/hb/search/stdHBDetailedCNF?id=8B1827F24BCDBB19E05397BE0A0AB44A"},
    {"code": "FZ/T 81007-2022", "name": "单、夹服装",
     "categories": ["休闲裤", "套装"], "excludes_infant": True,
     "scope": "普通机织单、夹服装的参考标准；有专门标准的款式另核对，不适用于36个月及以下婴幼儿服装",
     "source": "https://std.samr.gov.cn/hb/search/stdHBDetailed?id=F9A74BF1D2B7EC70E05397BE0A0A8DAE"},
    {"code": "GB/T 2660-2017", "name": "衬衫", "categories": ["衬衫", "半袖"],
     "scope": "机织衬衫的参考标准，包括符合适用范围的短袖衬衫；不能直接套用到针织T恤",
     "source": "https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=1A332C5B802EC31E7C1ED95E2E174C4B"},
    {"code": "FZ/T 73043-2020", "name": "针织衬衫", "categories": ["衬衫", "半袖"],
     "scope": "针织衬衫；区分针织T恤和机织衬衫，按厂家明示标准核对",
     "source": "https://std.samr.gov.cn/hb/search/stdHBDetailed?id=B9B5D16F8A6A8A9BE05397BE0A0A7DFC"},
    {"code": "GB/T 22849-2024", "name": "针织T恤衫", "categories": ["半袖"],
     "scope": "针织T恤衫；半袖若实际为衬衫，应另选对应衬衫标准，2014版已被替代",
     "source": "https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=0D814A3BC1F06628D101E8535F6C7997"},
    {"code": "GB/T 2662-2017", "name": "棉服装", "categories": ["棉服"],
     "scope": "带填充物的棉服装参考标准；不是普通含棉T恤的标准，须核对面料、填充物和原标签",
     "source": "https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=C314ED4F799F9EA552B22CDE31A1EDE3"},
    {"code": "FZ/T 73020-2019", "name": "针织休闲服装", "categories": ["休闲裤", "套装", "针织衫"],
     "scope": "针织休闲服装的参考标准，如符合适用范围的休闲裤、休闲套装；毛衫等有专门标准的产品另核对",
     "source": "https://std.samr.gov.cn/hb/search/stdHBDetailed?id=A3B7A90556B17E47E05397BE0A0A540E"},
    {"code": "FZ/T 73018-2021", "name": "毛针织品", "categories": ["针织衫"],
     "scope": "毛针织品；核对羊毛成份、含量及产品类型，不将所有针织衫视作毛针织品",
     "source": "https://std.samr.gov.cn/hb/search/stdHBDetailed?id=C3D579650BBEAF60E05397BE0A0AB567"},
    {"code": "FZ/T 73005-2021", "name": "低含毛混纺及仿毛针织品", "categories": ["针织衫"],
     "scope": "低含毛混纺及仿毛针织品；与毛针织品、山羊绒针织品区分，按厂家成份和采用标准核对",
     "source": "https://std.samr.gov.cn/hb/search/stdHBDetailed?id=C3D579650BBDAF60E05397BE0A0AB567"},
]
LABEL_USAGES = [
    {"value": "adult_skin", "name": "成人 · 直接接触皮肤", "minimum": "B", "standard": "GB 18401-2010",
     "description": "穿着时大部分面积直接接触皮肤，如半袖T恤、贴身衬衫、连衣裙、牛仔裤、休闲裤；贴身羊绒衫和针织衫也按此核对。"},
    {"value": "adult_outer", "name": "成人 · 非直接接触皮肤", "minimum": "C", "standard": "GB 18401-2010",
     "description": "穿着时不直接接触皮肤或仅小部分接触，如隔着内搭穿的风衣、羽绒服、羊绒大衣、棉服；毛衫应按实际穿着方式判断。"},
    {"value": "child_skin", "name": "儿童 · 直接接触皮肤", "minimum": "B", "standard": "GB 31701-2015",
     "description": "3岁以上、14岁及以下儿童的贴身T恤、衬衫、裙装、裤装等；还须符合儿童绳带、附件等额外安全要求。"},
    {"value": "child_outer", "name": "儿童 · 非直接接触皮肤", "minimum": "C", "standard": "GB 31701-2015",
     "description": "3岁以上、14岁及以下儿童隔着内搭穿的外套等；不能只凭成人产品的A/B/C类标识认定符合童装要求。"},
    {"value": "infant", "name": "婴幼儿 · 36个月及以下", "minimum": "A", "standard": "GB 31701-2015",
     "description": "36个月及以下婴幼儿用品无论贴身或外穿均须A类，并加注“婴幼儿用品”；须使用适用的产品标准。"},
]
SAFETY_CATEGORIES = [
    {"value": "A", "name": "A类 · 婴幼儿最低要求",
     "description": "婴幼儿用品必须达到A类；成人、儿童产品实际达到更高安全要求，也可按真实资料标A类。成人A类不等于已符合童装额外要求。",
     "examples": "婴幼儿毛衫、连体衣等；不能因材质为羊绒或价格高就标A类。"},
    {"value": "B", "name": "B类 · 直接接触皮肤最低要求",
     "description": "非婴幼儿、直接接触皮肤的产品至少达到B类，也可依据真实资料标A类；婴幼儿不能标B类。",
     "examples": "贴身半袖、衬衫、连衣裙、牛仔裤、休闲裤，以及贴身穿的羊绒衫、针织衫。"},
    {"value": "C", "name": "C类 · 非直接接触皮肤最低要求",
     "description": "非婴幼儿、非直接接触皮肤的产品至少达到C类；不能作为贴身产品或婴幼儿用品的安全类别。",
     "examples": "隔着内搭穿的风衣、羽绒服、羊绒大衣、棉服；外穿针织衫按实际接触方式核对。"},
]
LABEL_FIELDS = ("composition", "execution_standard", "label_usage", "safety_category", "label_verified")


def label_configuration(product):
    return {field: getattr(product, field, False if field == "label_verified" else None)
            for field in LABEL_FIELDS}


def normalize_standard(value):
    value = " ".join(str(value or "").split()).upper().replace("—", "-").replace("–", "-")
    return re.sub(r"^(GB(?:/T)?|FZ(?:/T)?)\s*(?=\d)", r"\1 ", value)


def normalize_category(value):
    if value is None:
        return None
    value = value.strip()
    return "连衣裙" if value == "莲衣裙" else value


def label_issues(config):
    issues = []
    if not (config.get("composition") or "").strip():
        issues.append("请填写原吊牌或供货资料中的成份")
    standard = normalize_standard(config.get("execution_standard"))
    if not standard:
        issues.append("请确认该商品的执行标准")
    elif not re.fullmatch(r"[A-Za-z][A-Za-z0-9 /_.-]*[0-9]", standard):
        issues.append("执行标准请填写完整标准号及年代号")
    usage = next((item for item in LABEL_USAGES if item["value"] == config.get("label_usage")), None)
    category = config.get("safety_category")
    if not usage:
        issues.append("请选择适用人群及接触类型")
    if category not in {"A", "B", "C"}:
        issues.append("请确认商品实际符合的安全技术类别")
    elif usage and "ABC".index(category) > "ABC".index(usage["minimum"]):
        issues.append(f"该使用类型至少需要{usage['minimum']}类，不能标为{category}类")
    selected = next((item for item in EXECUTION_STANDARDS if item["code"] == standard), None)
    if usage and usage["value"] == "infant" and selected and selected.get("excludes_infant"):
        issues.append("所选执行标准不适用于36个月及以下婴幼儿服装")
    if not config.get("label_verified"):
        issues.append("请由管理员核对原吊牌、供货资料或检测报告后确认")
    return issues


def safety_text(config):
    usage = next((item for item in LABEL_USAGES if item["value"] == config.get("label_usage")), None)
    if not usage or config.get("safety_category") not in {"A", "B", "C"}:
        return "待确认"
    return f"{usage['standard']} {config['safety_category']}类"


def standards_catalog():
    sources = [
        "https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=52C1F4CBDE863F5095D7C9D17F8E3F71",
        "https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=1698157554F00EED2E79EC6BFF7F4DF0",
    ]
    return {"checked_on": CHECKED_ON, "categories": PRODUCT_CATEGORIES,
            "execution_standards": EXECUTION_STANDARDS, "safety_categories": SAFETY_CATEGORIES,
            "usages": [{**usage, "source": sources[0 if usage["value"].startswith("adult") else 1]}
                       for usage in LABEL_USAGES], "safety_sources": sources}
