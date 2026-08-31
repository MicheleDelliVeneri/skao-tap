<resource schema="bench" resdir=".">
  <meta name="title">tap-compare ObsCore corpus</meta>
  <meta name="creationDate">2026-08-31T00:00:00</meta>
  <meta name="description">
    The benchmark's canonical ObsCore corpus (exported from egernia's
    ivoa.obscore view), published into DaCHS's ivoa.obscore exactly the way
    the DaCHS documentation publishes an obscore-shaped table: the
    //obscore#publishObscoreLike mixin, whose parameters default to the
    same-named columns this table deliberately carries.
  </meta>
  <meta name="subject">observational-astronomy</meta>
  <meta name="creator">tap-compare</meta>

  <table id="main" onDisk="True" adql="True">
    <FEED source="//obscore#obscore-columns"/>
    <mixin>//obscore#publishObscoreLike</mixin>
  </table>

  <data id="import">
    <sources>data/obscore.csv</sources>
    <csvGrammar/>
    <make table="main">
      <rowmaker idmaps="*">
        <apply name="nullify">
          <code>
            # CSV renders SQL NULL as the empty string; hand DaCHS real NULLs
            # so the numeric columns parse.
            for key, value in list(vars.items()):
              if value == '':
                vars[key] = None
          </code>
        </apply>
        <apply name="parse_region">
          <code>
            # egernia exports s_region as STC-S-ish text: "CIRCLE ra dec r".
            # DaCHS stores s_region as a pgsphere polygon; build it the way
            # DaCHS itself builds circle coverage.
            from gavo.utils import pgsphere
            raw = vars.get("s_region")
            if raw:
              parts = raw.split()
              vars["s_region"] = pgsphere.SCircle.fromDALI(
                [float(parts[-3]), float(parts[-2]), float(parts[-1])]).asPoly()
          </code>
        </apply>
      </rowmaker>
    </make>
  </data>
</resource>
