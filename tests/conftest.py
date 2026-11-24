import pytest

# The real saved link's reportState (no secrets inside: report name, columns, date range).
REPORT_STATE = (
    "CHoSDENhbGwgZGV0YWlscxqIAgrlAQoNQ2FsbFN0YXJ0VGltZQoMQ2FsbER1cmF0aW9uChhDYWxsZXJDb3VudHJ5"
    "Q2FsbGluZ0NvZGUKHENhbGxlck5hdGlvbmFsRGVzaWduYXRlZENvZGUKCENhbGxlcklkChNDYWxsUmVjb3JkaW5n"
    "UmVwbGF5ChNHb29nbGVWb2ljZUNhbGxUeXBlChtDYWxsVHJhY2tpbmdEaXNwbGF5TG9jYXRpb24KGEFkVGVsZXBo"
    "b255UHJvcGVydHlHcm91cAoIQ2FtcGFpZ24SFQoRCg8KDUNhbGxTdGFydFRpbWUQABgAIDIKACIcCBwSGAoKMjAy"
    "Ni0xMS0wMRIKMjAyNi0xMS0yNCIMGgoIAhgBKAA4AEgA"
)

# Same shape as the saved link, with dummy ids.
REPORT_URL = (
    "https://ads.google.com/aw/reporteditor/view?ocid=111&workspaceId=0&ascid=111"
    "&predefinedReportId=122&euid=222&__u=333&uscid=111&__c=444&authuser=0"
    f"&reportState={REPORT_STATE}"
)


@pytest.fixture
def report_state():
    return REPORT_STATE


@pytest.fixture
def report_url():
    return REPORT_URL
