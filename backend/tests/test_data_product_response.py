from datetime import UTC, datetime

from app.domain.models.data_product import DataProduct, DataProductFile
from app.interfaces.schemas.dataset import DataProductResponse


def test_data_product_response_accepts_domain_model() -> None:
    now = datetime.now(UTC)
    product = DataProduct(
        product_id="dp_test",
        dataset_id="dataset_test",
        source_session_id="session_test",
        name="GeoTIFF product",
        created_by="user_test",
        files=[
            DataProductFile(
                file_id="file_test",
                filename="result.tif",
                relative_path="result.tif",
                role="data",
                is_primary=True,
            )
        ],
        created_at=now,
        updated_at=now,
    )

    response = DataProductResponse.model_validate(product)

    assert response.product_id == "dp_test"
    assert response.files[0].filename == "result.tif"
    assert response.files[0].is_primary is True
