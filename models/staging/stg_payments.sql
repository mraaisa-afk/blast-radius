select
    id as payment_id,
    order_id,
    payment_method,
    amount
from {{ ref('raw_payments') }}
