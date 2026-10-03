# Page JSON Patch Store

纯 Python 标准库实现的页面 JSON 树条件补丁组件（内存态），供离线页面编辑器提交局部修改。

- `page_store.py`：`PageStore` 提供 `create` / `read` / `patch`，补丁遵循 RFC 6902（add/remove/replace/move/copy/test），路径遵循 RFC 6901，ETag 不匹配或最终页面树非法时整体回滚。
- `test_page_store.py`：自动化测试（unittest）。

## 运行测试

```sh
python3 -m unittest test_page_store -v
```
