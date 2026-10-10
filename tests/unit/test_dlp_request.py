from google.cloud import dlp_v2

from opsfleet_agent.gateways.dlp.redactor import build_request


def test_person_names_are_inspected_at_possible_while_addresses_stay_strict() -> None:
    request = build_request("projects/p/locations/l", "text")

    floors = {
        entry.info_type.name: entry.min_likelihood
        for entry in request.inspect_config.min_likelihood_per_info_type
    }
    assert request.inspect_config.min_likelihood == dlp_v2.Likelihood.POSSIBLE
    assert "PERSON_NAME" not in floors
    assert floors["STREET_ADDRESS"] == dlp_v2.Likelihood.LIKELY
    assert floors["DATE_OF_BIRTH"] == dlp_v2.Likelihood.LIKELY
