from django import forms


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        single_file_clean = super().clean

        if isinstance(data, (list, tuple)):
            return [single_file_clean(item, initial) for item in data]

        return single_file_clean(data, initial)


class NewRunForm(forms.Form):
    run_label = forms.CharField(
        max_length=255,
        required=False,
        label="Run label",
        widget=forms.TextInput(attrs={"placeholder": "Optional analyst label"}),
    )
    csv_files = MultipleFileField(
        label="AST CSV exports",
    )
    enable_cohort_mode = forms.BooleanField(
        required=False,
        initial=True,
        label="Enable cohort mode",
    )
    build_bundle = forms.BooleanField(
        required=False,
        initial=True,
        label="Build ZIP bundle",
    )
