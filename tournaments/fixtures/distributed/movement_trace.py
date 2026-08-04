def settle_movement(movement, provider_response):
    if provider_response.status_code == 202:
        movement.status = "settled"
    return movement
