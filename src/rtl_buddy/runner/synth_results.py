import pprint


class SynthResults:
    def __init__(self, name, results=None):
        if results is None:
            results = {"result": "NA", "desc": "NA"}
        self.name = name
        self.results = results
        if "result" not in results:
            results["result"] = "NA"
        if "desc" not in results:
            results["desc"] = "NA"

    def is_pass(self):
        return self.results["result"] in ("PASS", "SKIP")

    def __str__(self):
        return "synth_results: " + pprint.pformat(self.results)


class SynthPassResults(SynthResults):
    def __init__(self, name):
        super().__init__(
            name=name,
            results={"result": "PASS", "name": name, "desc": "Synthesis passed"},
        )


class SynthFailResults(SynthResults):
    def __init__(self, name, desc):
        super().__init__(
            name=name,
            results={"result": "FAIL", "name": name, "desc": desc},
        )


class SynthSkipResults(SynthResults):
    def __init__(self, name, desc):
        super().__init__(
            name=name,
            results={"result": "SKIP", "name": name, "desc": desc},
        )
