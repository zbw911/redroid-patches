import com.android.tools.smali.dexlib2.DexFileFactory;
import com.android.tools.smali.dexlib2.Opcodes;
import com.android.tools.smali.dexlib2.Opcode;
import com.android.tools.smali.dexlib2.builder.MutableMethodImplementation;
import com.android.tools.smali.dexlib2.builder.instruction.BuilderInstruction3rc;
import com.android.tools.smali.dexlib2.immutable.ImmutableClassDef;
import com.android.tools.smali.dexlib2.immutable.ImmutableMethod;
import com.android.tools.smali.dexlib2.immutable.reference.ImmutableMethodReference;
import com.android.tools.smali.dexlib2.writer.pool.DexPool;
import com.android.tools.smali.dexlib2.writer.io.FileDataStore;
import com.android.tools.smali.dexlib2.iface.*;
import java.io.File;
import java.util.*;

public final class DexPatch {
    static final String TARGET = "Lcom/android/systemui/statusbar/CommandQueue;";
    static final String EXPECTED = TARGET + "->onSystemBarAttributesChanged(II[Lcom/android/internal/view/AppearanceRegion;ZIILjava/lang/String;[Lcom/android/internal/statusbar/LetterboxDetails;)V";
    static String signature(Method method) {
        return method.getDefiningClass() + "->" + method.getName() + "("
                + String.join("", method.getParameterTypes()) + ")" + method.getReturnType();
    }
    public static void main(String[] args) throws Exception {
        if (args.length != 2) throw new IllegalArgumentException("Usage: DexPatch <source.apk> <patched.dex>");
        // Match this frozen APK's dex039 format. API35 selects 041 in dexlib, whose writer
        // does not produce the larger container header required by ART for that version.
        Opcodes originalFormat = Opcodes.forDexVersion(39);
        MultiDexContainer<?> container = DexFileFactory.loadDexContainer(new File(args[0]), originalFormat);
        int matched = 0;
        for (String entry : container.getDexEntryNames()) {
            DexFile dex = container.getEntry(entry).getDexFile();
            List<ClassDef> classes = new ArrayList<>();
            boolean modified = false;
            for (ClassDef cls : dex.getClasses()) {
                if (!TARGET.equals(cls.getType())) { classes.add(cls); continue; }
                List<Method> methods = new ArrayList<>();
                for (Method method : cls.getVirtualMethods()) {
                    if (!signature(method).equals(EXPECTED)) { methods.add(method); continue; }
                    matched++;
                    int count = 0;
                    for (Object ignored : method.getImplementation().getInstructions()) count++;
                    System.out.println("entry=" + entry);
                    System.out.println("method=" + signature(method));
                    System.out.println("registers=" + method.getImplementation().getRegisterCount());
                    System.out.println("instructions=" + count);
                    if (count != 22 || method.getImplementation().getRegisterCount() != 11)
                        throw new IllegalStateException("Original callback instructions/registers differ from pinned input");
                    MutableMethodImplementation impl = new MutableMethodImplementation(method.getImplementation());
                    ImmutableMethodReference bridge = new ImmutableMethodReference(
                            "Lcom/buildploy/statusbarprototype/AppearanceBridge;", "dispatch",
                            Arrays.asList("I", "I", "Ljava/lang/Object;", "Z", "I", "I", "Ljava/lang/String;", "Ljava/lang/Object;"), "V");
                    impl.addInstruction(0, new BuilderInstruction3rc(Opcode.INVOKE_STATIC_RANGE,
                            impl.getRegisterCount() - 8, 8, bridge));
                    methods.add(new ImmutableMethod(method.getDefiningClass(), method.getName(),
                            method.getParameters(), method.getReturnType(), method.getAccessFlags(),
                            method.getAnnotations(), method.getHiddenApiRestrictions(), impl));
                    modified = true;
                }
                classes.add(modified ? new ImmutableClassDef(cls.getType(), cls.getAccessFlags(),
                        cls.getSuperclass(), cls.getInterfaces(), cls.getSourceFile(), cls.getAnnotations(),
                        cls.getStaticFields(), cls.getInstanceFields(), cls.getDirectMethods(), methods) : cls);
            }
            if (modified) {
                DexPool pool = new DexPool(originalFormat);
                for (ClassDef cls : classes) pool.internClass(cls);
                FileDataStore store = new FileDataStore(new File(args[1]));
                try { pool.writeTo(store); } finally { store.close(); }
                System.out.println("patched=true");
            }
        }
        if (matched != 1) throw new IllegalStateException("Expected exactly one target, got " + matched);
    }
}
