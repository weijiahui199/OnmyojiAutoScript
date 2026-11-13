from tasks.GameUi.page import Page
from tasks.GlobalGame.assets import GlobalGameAssets
from tasks.MetaDemon.page import page_meta_demon
from tasks.MetaDemonShop.assets import MetaDemonShopAssets

# 解厄商店：入口在超鬼王主界面右下角，返回也是回到超鬼王主界面
page_meta_demon_shop = Page(MetaDemonShopAssets.I_SHOP_CHECK)
page_meta_demon.connect(page_meta_demon_shop, MetaDemonShopAssets.C_SHOP_ENTRY,
                        key="page_meta_demon->page_meta_demon_shop")
page_meta_demon_shop.connect(page_meta_demon, GlobalGameAssets.I_UI_BACK_YELLOW,
                             key="page_meta_demon_shop->page_meta_demon")
