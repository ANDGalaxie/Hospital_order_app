from django.db import migrations


def get_descendant_ids(ProductCategory, root_id):
    descendant_ids = []
    current_ids = [root_id]

    while current_ids:
        child_ids = list(
            ProductCategory.objects.filter(
                parent_id__in=current_ids,
            ).values_list("id", flat=True)
        )

        if not child_ids:
            break

        descendant_ids.extend(child_ids)
        current_ids = child_ids

    return descendant_ids


def link_unlinked_factory_nodes(apps, schema_editor):
    ProductCategory = apps.get_model(
        "products",
        "ProductCategory",
    )
    Product = apps.get_model(
        "products",
        "Product",
    )

    factory_nodes = ProductCategory.objects.filter(
        node_type="factory",
        factory__isnull=True,
    )

    for node in factory_nodes:
        descendant_ids = get_descendant_ids(
            ProductCategory,
            node.id,
        )

        # 使用 set 去重，避免 Product 默认 ordering 导致
        # distinct() 返回重复的 factory_id。
        factory_ids = sorted(
            set(
                Product.objects.filter(
                    category_id__in=descendant_ids,
                    factory_id__isnull=False,
                )
                .order_by()
                .values_list("factory_id", flat=True)
            )
        )

        if len(factory_ids) == 1:
            node.factory_id = factory_ids[0]
            node.save(update_fields=["factory"])


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0011_repair_unlinked_factory_nodes'),
    ]

    operations = [
        migrations.RunPython(
            link_unlinked_factory_nodes,
            migrations.RunPython.noop,
        ),
    ]
