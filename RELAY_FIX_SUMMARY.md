# Relay fix summary

- Stary alert był spowodowany utożsamieniem TradersPost Signal ID z broker order ID.
- v32.1 rozdziela `relay_signal_id` od `broker_order_id`.
- `cancelAfter` jest ustawiany na podstawie `ExecutionPlan.valid_until_ms`.
- `BROKER_FEEDBACK_MODE=traderspost` wyłącza fałszywy krytyczny ACK, ale nie udaje broker truth.
- Tryb `direct` pozostaje dostępny dla prawdziwego API/webhooka brokera.
