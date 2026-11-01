package serial

// Python's tracer once encoded a module as its attributes, and `os.sys.modules["nt"].environ` put
// the whole environment into trace files. Go has no module values, and this encoder reads exported
// fields only and calls no function to fetch anything: the environment lives in the syscall
// package's unexported state and is reached only by calling os.Environ. So a traced value carries
// the environment only when the app itself put it into one. These are the values a frame is most
// likely to hold that sit next to the process's own state; none may bring the environment with it.

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"strings"
	"testing"
)

const envSentinel = "fr-env-sentinel-7c1e9a"

func TestATracedValueCarriesNoEnvironmentTheAppDidNotPutInIt(t *testing.T) {
	t.Setenv("FR_TRACE_SENTINEL", envSentinel)
	self, _ := os.FindProcess(os.Getpid())
	values := map[string]any{
		"exec.Cmd":              exec.Command("go", "version"),
		"os.Process":            self,
		"os.ProcAttr":           &os.ProcAttr{},
		"os.Stdin":              os.Stdin,
		"http.DefaultClient":    http.DefaultClient,
		"http.DefaultTransport": http.DefaultTransport,
		"http.Request":          httptest.NewRequest("GET", "/", nil),
		"http.Server":           &http.Server{},
		"context":               context.Background(),
		"testing.T":             t,
		"os.Environ":            os.Environ,
		"a struct holding them": struct {
			Cmd  *exec.Cmd
			Proc *os.Process
			Ctx  context.Context
		}{exec.Command("go"), self, context.Background()},
	}
	for name, v := range values {
		b, err := json.Marshal(ToTraceJsonable(v))
		if err != nil {
			t.Errorf("%s: the traced form does not marshal: %v", name, err)
		}
		if strings.Contains(string(b), envSentinel) || strings.Contains(Render(v, 1<<20), envSentinel) {
			t.Errorf("tracing %s wrote the environment", name)
		}
	}

	// The control: the sentinel is detectable, and an environment the app copied into a value is
	// traced like any other data the app holds. Masking that is Redact's and Scrub's job.
	// One variable, not cmd.Environ(): a list is cut at TraceMaxItems, and a CI runner holds more
	// variables than that, so the sentinel appended last would fall outside the traced head.
	cmd := exec.Command("go", "version")
	cmd.Env = []string{"FR_TRACE_SENTINEL=" + os.Getenv("FR_TRACE_SENTINEL")}
	b, _ := json.Marshal(ToTraceJsonable(cmd))
	if !strings.Contains(string(b), envSentinel) {
		t.Fatalf("the sentinel is not detectable, so the assertions above prove nothing")
	}
}
