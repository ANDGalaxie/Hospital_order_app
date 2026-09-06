from django.db import migrations


def get_descendant_ids(ProductCategory, root_id):
    """
    获取一个分类节点下面的所有后代节点 ID。

    不能假设分类树永远只有三层，因此这里递归向下查找。
    """
    descendant_ids = []
    current_level_ids = [root_id]

    while current_level_ids:
        child_ids = list(
            ProductCategory.objects.filter(
                parent_id__in=current_level_ids,
            ).values_list("id", flat=True)
        )

        if not child_ids:
            break

        descendant_ids.extend(child_ids)
        current_level_ids = child_ids

    return descendant_ids


def link_factory_nodes_from_products(apps, schema_editor):
    """
    如果一个工厂节点下面的所有产品都指向同一个真实 Factory，
    就自动把该分类节点关联到这个 Factory。

    例如：
        分类节点：SINOMED
        下面产品的 factory：Sino Medical Sciences Technology Inc

    即使名称不同，也可以根据产品反向建立可靠关联。
    """
    ProductCategory = apps.get_model(
        "products",
        "ProductCategory",
    )
    Product = apps.get_model(
        "products",
        "Product",
    )

    unlinked_factory_nodes = ProductCategory.objects.filter(
        node_type="factory",
        factory__isnull=True,
    )

    for node in unlinked_factory_nodes:
        descendant_ids = get_descendant_ids(
            ProductCategory,
            node.id,
        )

        factory_ids = list(
            Product.objects.filter(
                category_id__in=descendant_ids,
                factory__isnull=False,
            )
            .values_list("factory_id", flat=True)
            .distinct()
        )

        # 只有所有产品唯一指向同一个 Factory 时才自动关联。
        if len(factory_ids) == 1:
            node.factory_id = factory_ids[0]
            node.save(update_fields=["factory"])


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0009_classify_product_category_nodes'),
    ]

    operations = [
        migrations.RunPython(
            link_factory_nodes_from_products,
            migrations.RunPython.noop,
        ),
    ]
