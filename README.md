# page_store

纯 Python 标准库实现的页面 JSON 树条件补丁组件（内存态），供离线页面编辑器提交局部修改。

## API

- `PageStore.create(document_json)`：创建页面，返回 `(page_id, etag)`。
- `PageStore.read(page_id)`：返回 `(document_json, etag)`。
- `PageStore.patch(page_id, expected_etag, patch_json)`：原子应用 RFC 6902 补丁，返回新 ETag。

语义要点：

- ETag 不匹配则整个补丁被拒绝；每次成功 Patch 都生成该页面从未用过的新 ETag（含空补丁、仅 test、值未变）。
- 补丁在文档深拷贝上顺序执行，任一步失败或最终页面树非法（缺字段/类型错/id 重复）时，文档与 ETag 保持调用前状态。
- 支持 RFC 6902 六种操作；`""` 指整个文档，允许 remove 根后再 add 重建；move 先删源再在结果文档中解释目标，移到自身是无改动（源须存在），移到自身后代失败。
- 路径段仅对 `~0`/`~1` 解码一次；对象成员按字符串精确匹配（含空串、数字、连字符）；数组下标为无前导零的非负十进制，插入允许等于长度，`-` 仅作插入目标表示追加。
- test 按 JSON 值比较：对象无序、数组有序、字符串区分大小写、布尔与数字不同、null 与缺失不同，数字按数值比较（1 == 1.0）。

## 运行测试

```sh
python3 -m unittest test_page_store -v
```
